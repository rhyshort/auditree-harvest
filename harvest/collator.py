# Copyright (c) 2020 IBM Corp. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Harvest file collator."""
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import PurePath
from urllib.parse import urlparse

import git

from harvest.exceptions import FileMissingError


class Collator(object):
    """Harvest collator to retrieve Git repository content."""

    def __init__(self, repo_url, creds, branch, repo_path=None, validate=True):
        """Construct the Collator object."""
        parsed = urlparse(repo_url)
        self.scheme = parsed.scheme
        self.hostname = parsed.hostname
        self.org, self.repo = parsed.path.strip("/").split("/")
        self.creds = creds
        self.branch = branch
        self.repo_path = repo_path
        self.git_repo = None
        self.validate = validate

    @property
    def local_path(self):
        """Provide the local OS path to the Git repo."""
        if self.repo_path:
            return self.repo_path
        tmpdir = PurePath(tempfile.gettempdir())
        return str(tmpdir.joinpath("harvest", self.org, self.repo))

    def read(self, filepath, from_dt, until_dt):
        """
        Retrieve commits from the repository based on a date range.

        On each iteration through the while loop iter_commits will return an
        iterator with a single entry.  That single entry will be the latest
        commit for the current_date which will be decremented accordingly until
        the looping condition is satisfied.

        :param str filepath: The relative path to the file within the repo
        :param datetime from_dt: The retrieval start date
        :param datetime until_dt: The retrieval end date

        :returns: A list of Commit objects
        """
        self.checkout()
        commits = {}
        current_date = until_dt + timedelta(days=1)

        expanded_file_paths = self._get_locker_file_paths(filepath)
        for ex_filepath in expanded_file_paths:
            while current_date > from_dt:
                try:
                    commit = next(
                        self.git_repo.iter_commits(
                            paths=ex_filepath, until=current_date, max_count=1
                        )
                    )
                    commits[commit.hexsha] = commit
                    current_date = datetime.strptime(
                        self._ts_to_str(commit.committed_date), "%Y%m%d"
                    )
                except StopIteration:
                    break
        if not commits:
            until = until_dt.strftime("%Y-%m-%d")
            since = from_dt.strftime("%Y-%m-%d")
            raise FileMissingError(f"{filepath} not found between {since} and {until}")

        return commits.values()

    def write(self, filepath, commits: list[git.Commit]):
        """
        Create file artifacts.

        :param str filepath: The relative path to the file within the repo
        :param list commits: A list of commits for a given file and date range
        """
        expanded_file_paths = self._get_locker_file_paths(filepath)
        for commit in commits:
            for ex_filepath in expanded_file_paths:
                file_name = (
                    f"./{self._ts_to_str(commit.committed_date)}_"
                    f'{ex_filepath.rsplit("/", 1).pop()}'
                )
                # need to check if the file is present in the commit first
                if ex_filepath in commit.tree:
                    with open(file_name, "w+") as f:
                        f.write(commit.tree[ex_filepath].data_stream.read().decode())

    def checkout(self):
        """Establish/Refresh the local Git repository."""
        if self.repo_path and not self.git_repo:
            self.git_repo = git.Repo(self.repo_path)
        if self.git_repo:
            if self.validate and not self._valid_repo():
                raise ValueError(f"{self.org}/{self.repo} repository mismatch")
            return
        if os.path.isdir(os.path.join(self.local_path, ".git")):
            try:
                self.git_repo = git.Repo(self.local_path)
                self.git_repo.remote().fetch()
                self.git_repo.remote().pull()
                return
            except git.exc.InvalidGitRepositoryError:
                shutil.rmtree(self.local_path)
        token = None
        if "github.com" in self.hostname:
            token = self.creds["github"].token
        elif "github" in self.hostname:
            token = self.creds["github_enterprise"].token
        elif "bitbucket" in self.hostname:
            token = self.creds["bitbucket"].token
        elif "gitlab" in self.hostname:
            token = self.creds["gitlab"].token
        url_path = f"{self.hostname}/{self.org}/{self.repo}.git"
        try:
            self.git_repo = git.Repo.clone_from(
                f"{self.scheme}://{token}@{url_path}",
                self.local_path,
                branch=self.branch,
            )
        except git.exc.GitCommandError as e:
            raise git.exc.GitCommandError(
                [c.replace(token, f'{"":*<10}') for c in e.command],
                e.status,
                e.stderr.strip("\n"),
            ) from None

    def _valid_repo(self):
        remote_url = self.git_repo.remotes.origin.url
        *_, org, repo = remote_url.split(".git").pop(0).rsplit("/", 2)
        return self.org == org.split(":").pop() and self.repo == repo

    def _ts_to_str(self, timestamp):
        return datetime.fromtimestamp(timestamp).strftime("%Y%m%d")

    def _get_locker_file_paths(self, filepath: str):
        dir = os.path.dirname(filepath)
        indexFile = os.path.join(dir, "index.json")
        requestedFileName = os.path.basename(filepath)
        index = json.load(self.git_repo.tree()[indexFile].data_stream)

        file_info = index[requestedFileName]
        partitions = file_info.get("partitions", None)

        if not partitions:
            return [filepath]

        # generate the list of file names we need to read
        filepaths = []
        for partition in partitions.keys():
            filepaths.append(os.path.join(dir, f"{partition}_{requestedFileName}"))

        return filepaths
