#!/usr/bin/env python3
import argparse
import json
import logging
import os
import shutil
import subprocess
import time
from typing import List, Optional, Set, Tuple, Union

from github import Github

from env_helper import GITHUB_WORKSPACE, RUNNER_TEMP
from s3_helper import S3Helper
from pr_info import PRInfo
from get_robot_token import get_best_robot_token, get_parameter_from_ssm
from upload_result_helper import upload_results
from commit_status_helper import post_commit_status
from clickhouse_helper import ClickHouseHelper, prepare_tests_results_for_clickhouse
from stopwatch import Stopwatch

NAME = "Push to Dockerhub (actions)"

TEMP_PATH = os.path.join(RUNNER_TEMP, "docker_images_check")


class DockerImage:
    def __init__(
        self,
        path: str,
        repo: str,
        parent: Optional["DockerImage"] = None,
        gh_repo_path: str = GITHUB_WORKSPACE,
    ):
        self.path = path
        self.full_path = os.path.join(gh_repo_path, path)
        self.repo = repo
        self.parent = parent
        self.built = False

    def __eq__(self, other):
        """Is used to check if DockerImage is in a set or not"""
        return self.path == other.path

    def __hash__(self):
        return hash(self.path)

    def __str__(self):
        return self.repo

    def __repr__(self):
        return f"DockerImage(path={self.path},path={self.path},parent={self.parent})"


def get_changed_docker_images(
    pr_info: PRInfo, repo_path: str, image_file_path: str
) -> Set[DockerImage]:
    images_dict = {}
    path_to_images_file = os.path.join(repo_path, image_file_path)
    if os.path.exists(path_to_images_file):
        with open(path_to_images_file, "rb") as dict_file:
            images_dict = json.load(dict_file)
    else:
        logging.info(
            "Image file %s doesnt exists in repo %s", image_file_path, repo_path
        )

    if not images_dict:
        return set()

    files_changed = pr_info.changed_files

    logging.info(
        "Changed files for PR %s @ %s: %s",
        pr_info.number,
        pr_info.sha,
        str(files_changed),
    )

    changed_images = []

    for dockerfile_dir, image_description in images_dict.items():
        for f in files_changed:
            if f.startswith(dockerfile_dir):
                name = image_description["name"]
                logging.info(
                    "Found changed file '%s' which affects "
                    "docker image '%s' with path '%s'",
                    f,
                    name,
                    dockerfile_dir,
                )
                changed_images.append(DockerImage(dockerfile_dir, name))
                break

    # The order is important: dependents should go later than bases, so that
    # they are built with updated base versions.
    index = 0
    while index < len(changed_images):
        image = changed_images[index]
        for dependent in images_dict[image.path]["dependent"]:
            logging.info(
                "Marking docker image '%s' as changed because it "
                "depends on changed docker image '%s'",
                dependent,
                image,
            )
            changed_images.append(DockerImage(dependent, image.repo, image))
        index += 1
        if index > 5 * len(images_dict):
            # Sanity check to prevent infinite loop.
            raise RuntimeError(
                f"Too many changed docker images, this is a bug. {changed_images}"
            )

    # With reversed changed_images set will use images with parents first, and
    # images without parents then
    result = set(reversed(changed_images))
    logging.info(
        "Changed docker images for PR %s @ %s: '%s'",
        pr_info.number,
        pr_info.sha,
        result,
    )
    return result


def gen_versions(
    pr_info: PRInfo, suffix: Optional[str]
) -> Tuple[List[str], Union[str, List[str]]]:
    pr_commit_version = str(pr_info.number) + "-" + pr_info.sha
    # The order is important, PR number is used as cache during the build
    versions = [str(pr_info.number), pr_commit_version]
    result_version = pr_commit_version
    if pr_info.number == 0:
        # First get the latest for cache
        versions.insert(0, "latest")

    if suffix:
        # We should build architecture specific images separately and merge a
        # manifest lately in a different script
        versions = [f"{v}-{suffix}" for v in versions]
        # changed_images_{suffix}.json should contain all changed images
        result_version = versions

    return versions, result_version


def build_and_push_one_image(
    image: DockerImage,
    version_string: str,
    push: bool,
    child: bool,
) -> Tuple[bool, str]:
    logging.info(
        "Building docker image %s with version %s from path %s",
        image.repo,
        version_string,
        image.full_path,
    )
    build_log = os.path.join(
        TEMP_PATH, f"build_and_push_log_{image.repo.replace('/', '_')}_{version_string}"
    )
    push_arg = ""
    if push:
        push_arg = "--push "

    from_tag_arg = ""
    if child:
        from_tag_arg = f"--build-arg FROM_TAG={version_string} "

    with open(build_log, "wb") as bl:
        cmd = (
            "docker buildx build --builder default "
            f"{from_tag_arg}"
            f"--build-arg BUILDKIT_INLINE_CACHE=1 "
            f"--tag {image.repo}:{version_string} "
            f"--cache-from type=registry,ref={image.repo}:{version_string} "
            f"{push_arg}"
            f"--progress plain {image.full_path}"
        )
        logging.info("Docker command to run: %s", cmd)
        with subprocess.Popen(cmd, shell=True, stderr=bl, stdout=bl) as proc:
            retcode = proc.wait()

        if retcode != 0:
            return False, build_log

    logging.info("Processing of %s successfully finished", image.repo)
    return True, build_log


def process_single_image(
    image: DockerImage,
    versions: List[str],
    push: bool,
    child: bool,
) -> List[Tuple[str, str, str]]:
    logging.info("Image will be pushed with versions %s", ", ".join(versions))
    result = []
    for ver in versions:
        for i in range(5):
            success, build_log = build_and_push_one_image(image, ver, push, child)
            if success:
                result.append((image.repo + ":" + ver, build_log, "OK"))
                break
            logging.info(
                "Got error will retry %s time and sleep for %s seconds", i, i * 5
            )
            time.sleep(i * 5)
        else:
            result.append((image.repo + ":" + ver, build_log, "FAIL"))

    logging.info("Processing finished")
    image.built = True
    return result


def process_image_with_parents(
    image: DockerImage, versions: List[str], push: bool, child: bool = False
) -> List[Tuple[str, str, str]]:
    result = []  # type: List[Tuple[str,str,str]]
    if image.built:
        return result

    if image.parent is not None:
        result += process_image_with_parents(image.parent, versions, push, False)
        child = True

    result += process_single_image(image, versions, push, child)
    return result


def process_test_results(
    s3_client: S3Helper, test_results: List[Tuple[str, str, str]], s3_path_prefix: str
) -> Tuple[str, List[Tuple[str, str]]]:
    overall_status = "success"
    processed_test_results = []
    for image, build_log, status in test_results:
        if status != "OK":
            overall_status = "failure"
        url_part = ""
        if build_log is not None and os.path.exists(build_log):
            build_url = s3_client.upload_test_report_to_s3(
                build_log, s3_path_prefix + "/" + os.path.basename(build_log)
            )
            url_part += f'<a href="{build_url}">build_log</a>'
        if url_part:
            test_name = image + " (" + url_part + ")"
        else:
            test_name = image
        processed_test_results.append((test_name, status))
    return overall_status, processed_test_results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Program to build changed or given docker images with all "
        "dependant images. Example for local running: "
        "python docker_images_check.py --no-push-images --no-reports "
        "--image-path docker/packager/binary",
    )

    parser.add_argument(
        "--suffix",
        type=str,
        help="suffix for all built images tags and resulting json file; the parameter "
        "significantly changes the script behavior, e.g. changed_images.json is called "
        "changed_images_{suffix}.json and contains list of all tags",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default="clickhouse",
        help="docker hub repository prefix",
    )
    parser.add_argument(
        "--image-path",
        type=str,
        action="append",
        help="list of image paths to build instead of using pr_info + diff URL, "
        "e.g. 'docker/packager/binary'",
    )
    parser.add_argument(
        "--no-reports",
        action="store_true",
        help="don't push reports to S3 and github",
    )
    parser.add_argument(
        "--no-push-images",
        action="store_true",
        help="don't push images to docker hub",
    )

    return parser.parse_args()


def main():
    logging.basicConfig(level=logging.INFO)
    stopwatch = Stopwatch()

    args = parse_args()
    if args.suffix:
        global NAME
        NAME += f" {args.suffix}"
        changed_json = os.path.join(TEMP_PATH, f"changed_images_{args.suffix}.json")
    else:
        changed_json = os.path.join(TEMP_PATH, "changed_images.json")

    push = not args.no_push_images
    if push:
        subprocess.check_output(  # pylint: disable=unexpected-keyword-arg
            "docker login --username 'robotclickhouse' --password-stdin",
            input=get_parameter_from_ssm("dockerhub_robot_password"),
            encoding="utf-8",
            shell=True,
        )

    if os.path.exists(TEMP_PATH):
        shutil.rmtree(TEMP_PATH)
    os.makedirs(TEMP_PATH)

    if args.image_path:
        pr_info = PRInfo()
        pr_info.changed_files = set(i for i in args.image_path)
    else:
        pr_info = PRInfo(need_changed_files=True)

    changed_images = get_changed_docker_images(
        pr_info, GITHUB_WORKSPACE, "docker/images.json"
    )
    logging.info("Has changed images %s", ", ".join([im.path for im in changed_images]))

    image_versions, result_version = gen_versions(pr_info, args.suffix)

    result_images = {}
    images_processing_result = []
    for image in changed_images:
        images_processing_result += process_image_with_parents(
            image, image_versions, push
        )
        result_images[image.repo] = result_version

    if changed_images:
        description = "Updated " + ",".join([im.repo for im in changed_images])
    else:
        description = "Nothing to update"

    if len(description) >= 140:
        description = description[:136] + "..."

    with open(changed_json, "w", encoding="utf-8") as images_file:
        json.dump(result_images, images_file)

    s3_helper = S3Helper("https://s3.amazonaws.com")

    s3_path_prefix = (
        str(pr_info.number) + "/" + pr_info.sha + "/" + NAME.lower().replace(" ", "_")
    )
    status, test_results = process_test_results(
        s3_helper, images_processing_result, s3_path_prefix
    )

    url = upload_results(s3_helper, pr_info.number, pr_info.sha, test_results, [], NAME)

    print(f"::notice ::Report url: {url}")
    print(f'::set-output name=url_output::"{url}"')

    if args.no_reports:
        return

    gh = Github(get_best_robot_token())
    post_commit_status(gh, pr_info.sha, NAME, description, status, url)

    prepared_events = prepare_tests_results_for_clickhouse(
        pr_info,
        test_results,
        status,
        stopwatch.duration_seconds,
        stopwatch.start_time_str,
        url,
        NAME,
    )
    ch_helper = ClickHouseHelper()
    ch_helper.insert_events_into(db="gh-data", table="checks", events=prepared_events)


if __name__ == "__main__":
    main()
