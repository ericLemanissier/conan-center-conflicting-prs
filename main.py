import requests
import datetime
import os
from multiprocessing import Pool

def _get_diff(pr):
    r = requests.get(pr["diff_url"])
    r.raise_for_status()
    return r.text

def _make_request(method, url, **kwargs):
    token = os.getenv("GH_TOKEN")
    headers = {}
    if token:
        headers["Authorization"]=  "token %s" % token

    headers["Accept"] = "application/vnd.github.v3+json"
    
    user = os.getenv("GH_USER")
    pw = os.getenv("GH_PW")
    auth = None
    if user and pw:
        auth = requests.auth.HTTPBasicAuth(user, pw)
    r = requests.request(method, "https://api.github.com" + url, headers=headers, auth=auth, **kwargs)
    r.raise_for_status()
    if int(r.headers["X-RateLimit-Remaining"]) < 10:
        print("%s/%s github api call used, remaining %s until %s" % (r.headers["X-Ratelimit-Used"], r.headers["X-RateLimit-Limit"], r.headers["X-RateLimit-Remaining"], datetime.datetime.fromtimestamp(int(r.headers["X-Ratelimit-Reset"]))))
    return r

def main():
    owner = "conan-io"
    repo = "conan-center-index"

    prs = list()

    page = 1
    while True:
        r = _make_request("GET",f"/repos/{owner}/{repo}/pulls", params=
        {
            "state": "open",
            "sort": "created",
            "direction": "desc",
            "per_page": 100,
            "page": str(page)
        })
        results = r.json()
        prs.extend(results)
        page += 1
        if not results:
            break
        
    with Pool(os.cpu_count()) as p:
        status_futures = [
            p.apply_async(_get_diff, (pr,))
            for pr in prs
        ]
        for i in range(len(prs)):
            prs[i]["diff"] = status_futures[i].get()

    for p in prs:
        p["libs"] = set()
        for l in p["diff"].split("\n"):
            if l.startswith("+++ b/recipes/") or l.startswith("--- a/recipes/"):
                l = l.split("/")
                p["libs"].add(l[2])

    libs = dict()

    illegal_prs = list()

    for pr in prs:
        if len(pr["libs"]) > 1:
            illegal_prs.append(pr)
        else:
            for lib in pr["libs"]:
                if not lib in libs:
                    libs[lib] = list()
                libs[lib].append(pr["number"])


    msg = "The following table lists all the pull requests modifying files belonging to the same recipe.\n"
    msg += "It is automatically generated by https://github.com/ericLemanissier/conan-center-conflicting-prs so don't hesitate to report issues/improvements there.\n"
    msg += "| Library | Pull requests |\n"
    msg += "| --- | --- |\n"
    for l in sorted(libs):
        if len(libs[l]) > 1:
            msg += "| %s | " % l
            msg += ", ".join(["#%s" % pr for pr in libs[l]])
            msg += " |\n"
    
    msg += "\n"
    msg += "\n"
    msg += "The following pull requests modify several recipes, so they were ignored:\n"
    msg += "| Pull request | Libraries |\n"
    msg += "| --- | --- |\n"
    for p in illegal_prs:
        msg += "| #%s | " % p["number"]
        msg += ", ".join(sorted(p["libs"]))
        msg += " |\n"

    print(msg)

    issue_number = os.getenv("GH_ISSUE_NUMBER")
    if issue_number:    
        print("updating issue")
        _make_request("PATCH", f"/repos/{owner}/{repo}/issues/{issue_number}", json=
        {
            "body": msg,
        })
                
    user_id = _make_request("GET", f"/user").json()["id"]
        
    def _post_message_for_lib(issue_number, l):
        message = "I detected other pull requests that are modifying %s recipe:\n" % l
        for pr in libs[l]:
            if pr != issue_number:
                message += "- #%s\n" % pr
        message += "\n"
        message += "This message is automatically generated by https://github.com/ericLemanissier/conan-center-conflicting-prs so don't hesitate to report issues/improvements there.\n"

        def _get_comment_id(issue_number, user_id):
            page = 1
            while True:
                r = _make_request("GET", f"/repos/{owner}/{repo}/issues/{issue_number}/comments", params=
                {
                    "per_page": 100,
                    "page": str(page)
                })
                results = r.json()
                for c in results:
                    if c["user"]["id"] == user_id:
                        return c["id"]
                page += 1
                if not results:
                    break
            return None

        comment_id = _get_comment_id(issue_number, user_id)
        if comment_id:                   
            print(f"comment found: https://github.com/{owner}/{repo}/pull/{issue_number}#issuecomment-{comment_id}") 
            _make_request("PATCH", f"/repos/{owner}/{repo}/issues/comments/{comment_id}",json=
            {
                "body": message
            })
        else:
            print(f"Comment not found, creating one in https://github.com/{owner}/{repo}/issues/{issue_number}")
            _make_request("POST", f"/repos/{owner}/{repo}/issues/{issue_number}/comments", json=
            {
                "body": message
            })
    
    for l in libs:
        if len(libs[l]) <= 1:
            continue
        for issue_number in libs[l]:
            _post_message_for_lib(issue_number, l)
                    

if __name__ == "__main__":
    # execute only if run as a script
    main()