import requests
import datetime
import os
from multiprocessing import Pool


def _get_diff(pr):
    r = requests.get(pr["diff_url"])
    r.raise_for_status()
    return r.text


class Detector:
    owner = "conan-io"
    repo = "conan-center-index"

    dry_run = False

    def __init__(self, token=None, user=None, password=None):
        self.token = token
        self.user = user
        self.pw = password

        self.prs = {}

        page = 1
        while True:
            r = self._make_request("GET", f"/repos/{self.owner}/{self.repo}/pulls", params={
                "state": "open",
                "sort": "created",
                "direction": "desc",
                "per_page": 100,
                "page": str(page)
            })
            results = r.json()
            for p in results:
                self.prs[int(p["number"])] = p
            page += 1
            if not results:
                break

        with Pool(os.cpu_count()) as p:
            status_futures = {}
            for pr in self.prs:
                status_futures[pr] = p.apply_async(_get_diff, (self.prs[pr],))
            for pr in self.prs:
                self.prs[pr]["diff"] = status_futures[pr].get()

        for p in self.prs.values():
            p["libs"] = set()
            for line in p["diff"].split("\n"):
                if line.startswith("+++ b/recipes/") or line.startswith("--- a/recipes/"):
                    p["libs"].add(line.split("/")[2])

        self.libs = dict()

        self.illegal_prs = list()

        for pr in self.prs.values():
            if len(pr["libs"]) > 1:
                self.illegal_prs.append(pr)
            else:
                for lib in pr["libs"]:
                    if lib not in self.libs:
                        self.libs[lib] = list()
                    self.libs[lib].append(pr["number"])

        self.user_id = self._make_request("GET", f"/user").json()["id"]

    def _make_request(self, method, url, **kwargs):
        if self.dry_run and method in ["PATCH", "POST"]:
            return
        headers = {}
        if self.token:
            headers["Authorization"] = "token %s" % self.token

        headers["Accept"] = "application/vnd.github.v3+json"

        auth = None
        if self.user and self.pw:
            auth = requests.auth.HTTPBasicAuth(self.user, self.pw)
        r = requests.request(method, "https://api.github.com%s" % url, headers=headers, auth=auth, **kwargs)
        r.raise_for_status()
        if int(r.headers["X-RateLimit-Remaining"]) < 10:
            print("%s/%s github api call used, remaining %s until %s" % (
                r.headers["X-Ratelimit-Used"], r.headers["X-RateLimit-Limit"], r.headers["X-RateLimit-Remaining"],
                datetime.datetime.fromtimestamp(int(r.headers["X-Ratelimit-Reset"]))))
        return r

    def update_issue(self, issue_number):
        msg = "The following table lists all the pull requests modifying files belonging to the same recipe.\n"
        msg += "It is automatically generated by https://github.com/ericLemanissier/conan-center-conflicting-prs "
        msg += "so don't hesitate to report issues/improvements there.\n"
        msg += "| Library | Pull requests |\n"
        msg += "| --- | --- |\n"
        for lib_name in sorted(self.libs):
            if len(self.libs[lib_name]) > 1:
                msg += "| %s | " % lib_name
                msg += ", ".join(["#%s" % pr for pr in self.libs[lib_name]])
                msg += " |\n"

        msg += "\n"
        msg += "\n"
        msg += "The following pull requests modify several recipes, so they were ignored:\n"
        msg += "| Pull request | Libraries |\n"
        msg += "| --- | --- |\n"
        for p in self.illegal_prs:
            msg += "| #%s | " % p["number"]
            msg += ", ".join(sorted(p["libs"]))
            msg += " |\n"
        print(msg)

        if issue_number and self._make_request("GET", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}").json()["body"] != msg:
            print("updating issue")
            self._make_request("PATCH", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}", json={
                "body": msg,
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

        def _all_prs_referenced_in_body():
            for pr in conflicting_prs:
                if ("#%s" % pr) not in self.prs[issue_number]["body"]:
                    return False
            return True

        if _all_prs_referenced_in_body():
            print("all the conflicting prs (%s) are already referenced in #%s, skipping message" % (
                ", ".join("#%s" % p for p in conflicting_prs), issue_number))
            return

        message = "I detected other pull requests that are modifying %s recipe:\n" % lib_name
        message += "".join(["- #%s\n" % pr for pr in conflicting_prs])
        message += "\n"
        message += "This message is automatically generated by https://github.com/ericLemanissier/conan-center-conflicting-prs so don't hesitate to report issues/improvements there.\n"

        comment_id = self._get_comment_id(issue_number)
        if comment_id:
            if comment_id["body"] != message:
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
        for lib_name in self.libs:
            if len(self.libs[lib_name]) <= 1:
                continue
            for issue_number in self.libs[lib_name]:
                self._post_message_for_lib(issue_number, lib_name)


def main():
    d = Detector(token=os.getenv("GH_TOKEN"))
    d.update_issue(os.getenv("GH_ISSUE_NUMBER"))
    d.update_pr_messages()


if __name__ == "__main__":
    # execute only if run as a script
    main()
