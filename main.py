#pylint: disable = line-too-long, missing-module-docstring, missing-class-docstring, missing-function-docstring, invalid-name, too-many-lines, too-many-branches, no-name-in-module, too-few-public-methods, too-many-locals

from datetime import datetime,timedelta,timezone
import os
import logging
import requests
import dateutil.parser


class Detector:
    owner = "conan-io"
    repo = "conan-center-index"

    dry_run = False

    def __init__(self, token=None, user=None, pw=None):
        self.session = requests.session()

        if user and pw:
            self.session.auth = requests.auth.HTTPBasicAuth(user, pw)

        self.session.headers = {}
        if token:
            self.session.headers["Authorization"] = f"token {token}"

        self.session.headers["Accept"] = "application/vnd.github.v3+json"
        self.session.headers["User-Agent"] = "request"

        self.prs = {}

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

        for pr_number, pr in self.prs.items():
            pr["libs"] = set()
            for file in self._make_request("GET", f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/files").json():
                for field in ['filename', 'previous_filename']:
                    parts = file.get(field, '').split("/")
                    if len(parts) >= 4 and parts[0] == "recipes":
                        pr["libs"].add(f"{parts[1]}/{parts[2]}")

        self.libs = {}

        self.illegal_prs = []

        for pr in self.prs.values():
            if len(pr["libs"]) > 1:
                def get_package_name(e):
                    return e.split('/')[0]
                libs = pr["libs"].copy()
                package_name = get_package_name(libs.pop())
                if any(get_package_name(l) != package_name for l in libs):
                    self.illegal_prs.append(pr)
                    continue

            for lib in pr["libs"]:
                if lib not in self.libs:
                    self.libs[lib] = []
                self.libs[lib].append(pr["number"])

        if not self.dry_run:
            self.user_id = self._make_request("GET", "/user").json()["id"]

    def _make_request(self, method, url, **kwargs):
        if self.dry_run and method in ["PATCH", "POST"]:
            return None

        r = self.session.request(method, f"https://api.github.com{url}", **kwargs)
        r.raise_for_status()
        if int(r.headers["X-RateLimit-Remaining"]) < 10:
            logging.warning("%s/%s github api call used, remaining %s until %s",
                r.headers["X-Ratelimit-Used"], r.headers["X-RateLimit-Limit"], r.headers["X-RateLimit-Remaining"],
                datetime.fromtimestamp(int(r.headers["X-Ratelimit-Reset"])))
        return r

    def update_issue(self, issue_number):
        msg = "The following table lists all the pull requests modifying files belonging to the same recipe.\n"
        msg += "It is automatically generated by https://github.com/ericLemanissier/conan-center-conflicting-prs "
        msg += "so don't hesitate to report issues/improvements there.\n\n"
        msg += "| Library | Pull requests |\n"
        msg += "| --- | --- |\n"
        for lib_name in sorted(self.libs):
            if len(self.libs[lib_name]) > 1:
                msg += f"| {lib_name} | "
                msg += ", ".join([f"[#{pr}](https://github.com/conan-io/conan-center-index/pull/{pr})" for pr in self.libs[lib_name]])
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
        print(msg)


        with open("index.md", "w", encoding="latin_1") as text_file:
            text_file.write(msg)
            text_file.write("\npage generated on {{ site.time | date_to_xmlschema }}\n\n")

        if issue_number and self._make_request("GET", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}").json()["body"] != msg:
            print("updating issue")
            self._make_request("PATCH", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}", json={
                "body": msg + "\nThis can also be viewed on https://ericlemanissier.github.io/conan-center-conflicting-prs/\n\n",
            })

    def _get_comment_id(self, issue_number):
        page = 1
        while True:
            r = self._make_request("GET", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments", params={
                "per_page": 100,
                "page": str(page)
            })
            results = r.json()
            for c in results:
                if c["user"]["id"] == self.user_id:
                    return c
            page += 1
            if not results:
                break
        return None

    def _post_message_for_lib(self, issue_number, lib_name):
        conflicting_prs = [pr for pr in self.libs[lib_name] if pr != issue_number]

        def _all_prs_referenced_in_message(message):
            if not message:
                return False
            return all((f"#{pr}") in message or (f"/{pr}") in message for pr in conflicting_prs)

        if _all_prs_referenced_in_message(self.prs[issue_number]["body"]):
            logging.warning("all the conflicting prs (%s) are already referenced in #%s, skipping message",
                ", ".join(f"#{p}" for p in conflicting_prs), issue_number)
            return

        message = f"I detected other pull requests that are modifying {lib_name} recipe:\n"
        message += "".join([f"- #{pr}\n" for pr in conflicting_prs])
        message += "\n"
        message += "This message is automatically generated by https://github.com/ericLemanissier/conan-center-conflicting-prs so don't hesitate to report issues/improvements there.\n"

        if not self.dry_run:
            comment_id = self._get_comment_id(issue_number)
            if comment_id:
                if not _all_prs_referenced_in_message(comment_id["body"]):
                    print(
                        f"comment found: https://github.com/{self.owner}/{self.repo}/pull/{issue_number}#issuecomment-%s" % comment_id['id'])
                    self._make_request("PATCH", f"/repos/{self.owner}/{self.repo}/issues/comments/%s" % comment_id["id"], json={
                        "body": message
                })
            else:
                print(
                    f"Comment not found, creating one in https://github.com/{self.owner}/{self.repo}/issues/{issue_number}")
                self._make_request("POST", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments", json={
                    "body": message
                })

    def update_pr_messages(self):
        for lib_name, libs in self.libs.items():
            if len(libs) <= 1:
                continue
            for issue_number in libs:
                if any(label["name"] == "stale" for label in self.prs[issue_number]["labels"]):
                    logging.warning("skipping %s message because PR is stale", issue_number)
                    continue
                if dateutil.parser.isoparse(self.prs[issue_number]["updated_at"]) < datetime.now(timezone.utc) - timedelta(days=15):
                    logging.warning("skipping %s message because PR has not been updated since %s", issue_number, self.prs[issue_number]["updated_at"])
                    continue
                self._post_message_for_lib(issue_number, lib_name)


def main():
    d = Detector(token=os.getenv("GH_TOKEN"))
    d.update_issue(os.getenv("GH_ISSUE_NUMBER"))
    d.update_pr_messages()


if __name__ == "__main__":
    # execute only if run as a script
    main()
