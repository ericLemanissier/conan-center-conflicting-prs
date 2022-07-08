import requests
from datetime import datetime,timedelta,timezone
import os
import aiohttp, asyncio
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
            self.session.headers["Authorization"] = "token %s" % token

        self.session.headers["Accept"] = "application/vnd.github.v3+json"
        self.session.headers["User-Agent"] = "request"

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


        async def _populate_diffs():
            async with aiohttp.ClientSession() as session:
                async def _populate_diff(pr):
                    async with session.get(self.prs[pr]["diff_url"]) as r:
                        r.raise_for_status()
                        self.prs[pr]["libs"] = set()
                        try:
                            diff = await r.text()
                        except UnicodeDecodeError:
                            print("error when decoding diff at %s" % self.prs[pr]["diff_url"])
                            return
                        for line in diff.split("\n"):
                            if line.startswith("+++ b/recipes/") or line.startswith("--- a/recipes/"):
                                parts = line.split("/")
                                if len(parts) >= 5:
                                    self.prs[pr]["libs"].add("%s/%s" % (parts[2], parts[3]))
                await asyncio.gather(*[asyncio.create_task(_populate_diff(pr)) for pr in self.prs])

        loop = asyncio.get_event_loop()
        loop.run_until_complete(_populate_diffs())

        self.libs = dict()

        self.illegal_prs = list()

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
                    self.libs[lib] = list()
                self.libs[lib].append(pr["number"])

        if not self.dry_run:
            self.user_id = self._make_request("GET", f"/user").json()["id"]

    def _make_request(self, method, url, **kwargs):
        if self.dry_run and method in ["PATCH", "POST"]:
            return

        r = self.session.request(method, "https://api.github.com%s" % url, **kwargs)
        r.raise_for_status()
        if int(r.headers["X-RateLimit-Remaining"]) < 10:
            print("%s/%s github api call used, remaining %s until %s" % (
                r.headers["X-Ratelimit-Used"], r.headers["X-RateLimit-Limit"], r.headers["X-RateLimit-Remaining"],
                datetime.fromtimestamp(int(r.headers["X-Ratelimit-Reset"]))))
        return r

    def update_issue(self, issue_number):
        msg = "The following table lists all the pull requests modifying files belonging to the same recipe.\n"
        msg += "It is automatically generated by https://github.com/ericLemanissier/conan-center-conflicting-prs "
        msg += "so don't hesitate to report issues/improvements there.\n\n"
        msg += "| Library | Pull requests |\n"
        msg += "| --- | --- |\n"
        for lib_name in sorted(self.libs):
            if len(self.libs[lib_name]) > 1:
                msg += "| %s | " % lib_name
                msg += ", ".join([f"[#{pr}](https://github.com/conan-io/conan-center-index/pull/{pr})" for pr in self.libs[lib_name]])
                msg += " |\n"

        if self.illegal_prs:
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

        
        with open("index.md", "w") as text_file:
            text_file.write(msg)
            text_file.write(f"\npage generated on {{ site.time | date_to_xmlschema }}\n\n")

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
            return all(("#%s" % pr) in message or ("/%s" % pr) in message for pr in conflicting_prs)

        if _all_prs_referenced_in_message(self.prs[issue_number]["body"]):
            print("all the conflicting prs (%s) are already referenced in #%s, skipping message" % (
                ", ".join("#%s" % p for p in conflicting_prs), issue_number))
            return

        message = "I detected other pull requests that are modifying %s recipe:\n" % lib_name
        message += "".join(["- #%s\n" % pr for pr in conflicting_prs])
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
        for lib_name in self.libs:
            if len(self.libs[lib_name]) <= 1:
                continue
            for issue_number in self.libs[lib_name]:
                if any(label["name"] == "stale" for label in self.prs[issue_number]["labels"]):
                    print("skipping %s message because PR is stale" % issue_number)
                    continue
                if dateutil.parser.isoparse(self.prs[issue_number]["updated_at"]) < datetime.now(timezone.utc) - timedelta(days=15):
                    print("skipping %s message because PR has not been updated since %s" % (issue_number, self.prs[issue_number]["updated_at"]))
                    continue
                self._post_message_for_lib(issue_number, lib_name)


def main():
    d = Detector(token=os.getenv("GH_TOKEN"))
    d.update_issue(os.getenv("GH_ISSUE_NUMBER"))
    d.update_pr_messages()


if __name__ == "__main__":
    # execute only if run as a script
    main()
