# pylint: disable = invalid-name

from datetime import datetime, timedelta, timezone
import os
from typing import Any, Dict, List, Set
import logging
import requests
import dateutil.parser


class Detector:
    owner: str = "conan-io"
    repo: str = "conan-center-index"

    dry_run: bool = False

    def __init__(self, token: str = "", user: str = "", pw: str = ""):
        self.session = requests.session()

        if user and pw:
            self.session.auth = requests.auth.HTTPBasicAuth(user, pw)

        if token:
            self.session.headers["Authorization"] = f"token {token}"

        self.session.headers["Accept"] = "application/vnd.github.v3+json"
        self.session.headers["User-Agent"] = "request"

        self.prs: Dict[int, Dict[str, Any]] = {}

        self._get_all_prs()

        for pr_number, pr in self.prs.items():
            pr["libs"] = self._get_modified_libs_for_pr(pr_number)

        self.libs: Dict[str, List[int]] = {}

        self.illegal_prs: List[dict] = []

        for pr in self.prs.values():
            self._process_pr(pr)

        if not self.dry_run:
            self.user_id = self._make_request("GET", "/user").json()["id"]

    def _get_all_prs(self) -> None:
        page = 1
        while True:
            results = self._make_request("GET", f"/repos/{self.owner}/{self.repo}/pulls", params={
                "state": "open",
                "sort": "created",
                "direction": "desc",
                "per_page": 100,
                "page": str(page)
            }).json()
            for p in results:
                self.prs[int(p["number"])] = p
            page += 1
            if not results:
                break

    def _get_modified_libs_for_pr(self, pr: int) -> Set[str]:
        res = set()
        for file in self._make_request("GET", f"/repos/{self.owner}/{self.repo}/pulls/{pr}/files").json():
            for field in ['filename', 'previous_filename']:
                parts = file.get(field, '').split("/")
                if len(parts) >= 4 and parts[0] == "recipes":
                    res.add(f"{parts[1]}/{parts[2]}")
        return res

    def _process_pr(self, pr: Dict[str, Any]) -> None:
        if len(pr["libs"]) > 1:
            def get_package_name(e: str) -> str:
                return e.split('/')[0]
            libs = pr["libs"].copy()
            package_name = get_package_name(libs.pop())
            if any(get_package_name(lib) != package_name for lib in libs):
                self.illegal_prs.append(pr)
                return

        for lib in pr["libs"]:
            if lib not in self.libs:
                self.libs[lib] = []
            self.libs[lib].append(pr["number"])

    def _make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        if self.dry_run and method in ["PATCH", "POST"]:
            return requests.Response()

        r = self.session.request(method, f"https://api.github.com{url}", **kwargs)
        r.raise_for_status()
        if int(r.headers["X-RateLimit-Remaining"]) < 10:
            logging.warning("%s/%s github api call used, remaining %s until %s",
                            r.headers["X-Ratelimit-Used"], r.headers["X-RateLimit-Limit"], r.headers["X-RateLimit-Remaining"],
                            datetime.fromtimestamp(int(r.headers["X-Ratelimit-Reset"])))
        return r

    def update_issue(self, issue_number: str) -> None:
        msg = "The following table lists all the pull requests modifying files belonging to the same recipe.\n"
        msg += "It is automatically generated by https://github.com/ericLemanissier/conan-center-conflicting-prs "
        msg += "so don't hesitate to report issues/improvements there.\n\n"
        msg += "| Library | Pull requests |\n"
        msg += "| --- | --- |\n"
        for lib_name in sorted(self.libs):
            if len(self.libs[lib_name]) > 1:
                msg += f"| {lib_name} | "
                msg += ", ".join([f"[#{pr}](https://github.com/{self.owner}/{self.repo}/pull/{pr})" for pr in self.libs[lib_name]])
                msg += " |\n"

        if self.illegal_prs:
            msg += "\n"
            msg += "\n"
            msg += "The following pull requests modify several recipes, so they were ignored:\n"
            msg += "| Pull request | Libraries |\n"
            msg += "| --- | --- |\n"
            for p in self.illegal_prs:
                msg += f"| #{p['number']} | "
                msg += ", ".join(sorted(p["libs"]))
                msg += " |\n"
        logging.debug(msg)

        with open("index.md", "w", encoding="latin_1") as text_file:
            text_file.write(msg)
            text_file.write("\npage generated on {{ site.time | date_to_xmlschema }}\n\n")

        if issue_number and self._make_request("GET", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}").json()["body"] != msg:
            logging.debug("updating issue")
            self._make_request("PATCH", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}", json={
                "body": msg + "\nThis can also be viewed on https://ericlemanissier.github.io/conan-center-conflicting-prs/\n\n",
            })

    def _get_comment_id(self, issue_number: int, prefix: str) -> Dict[str, str]:
        page = 1
        while True:
            results = self._make_request("GET", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments", params={
                "per_page": 100,
                "page": str(page)
            }).json()
            for c in results:
                if c["user"]["id"] == self.user_id and c["body"].startswith(prefix):
                    return c
            page += 1
            if not results:
                break
        return {}

    def _post_message_for_lib(self, issue_number: int, lib_name: str) -> None:
        conflicting_prs = [pr for pr in self.libs[lib_name] if pr != issue_number]

        def _all_prs_referenced_in_message(message: str) -> bool:
            if not message:
                return False
            return all((f"#{pr}") in message or (f"/{pr}") in message for pr in conflicting_prs)

        if _all_prs_referenced_in_message(self.prs[issue_number]["body"]):
            logging.warning("all the conflicting prs (%s) are already referenced in #%s, skipping message",
                            ", ".join(f"#{p}" for p in conflicting_prs), issue_number)
            return

        first_line = f"I detected other pull requests that are modifying {lib_name} recipe:\n"
        message = first_line
        message += "".join([f"- #{pr}\n" for pr in conflicting_prs])
        message += "\n"
        message += "This message is automatically generated by https://github.com/ericLemanissier/conan-center-conflicting-prs "
        message += "so don't hesitate to report issues/improvements there.\n"

        if not self.dry_run:
            comment_id = self._get_comment_id(issue_number, first_line)
            if comment_id:
                if not _all_prs_referenced_in_message(comment_id["body"]):
                    logging.debug("comment found: https://github.com/%s/%s/pull/%s#issuecomment-%s",
                                  self.owner, self.repo, issue_number, comment_id['id'])
                    self._make_request("PATCH", f"/repos/{self.owner}/{self.repo}/issues/comments/%s" % comment_id["id"],
                                       json={"body": message})
            else:
                logging.debug("Comment not found, creating one in https://github.com/%s/%s/issues/%s",
                              self.owner, self.repo, issue_number)
                self._make_request("POST", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments", json={
                    "body": message
                })

    def update_pr_messages(self) -> None:
        for lib_name, libs in self.libs.items():
            if len(libs) <= 1:
                continue
            for issue_number in libs:
                if any(label["name"] == "stale" for label in self.prs[issue_number]["labels"]):
                    logging.warning("skipping %s message because PR is stale", issue_number)
                    continue
                if dateutil.parser.isoparse(self.prs[issue_number]["updated_at"]) < datetime.now(timezone.utc) - timedelta(days=15):
                    logging.warning("skipping %s message because PR has not been updated since %s",
                                    issue_number, self.prs[issue_number]["updated_at"])
                    continue
                self._post_message_for_lib(issue_number, lib_name)


def main():
    d = Detector(token=os.getenv("GH_TOKEN"))
    d.update_issue(os.getenv("GH_ISSUE_NUMBER"))
    d.update_pr_messages()


if __name__ == "__main__":
    # execute only if run as a script
    main()
