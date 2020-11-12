import requests
import datetime
import os
from multiprocessing import Pool

def _get_diff(pr):
    r = requests.get(pr["diff_url"])
    r.raise_for_status()
    return r.text

def main():

    token = os.getenv("GH_TOKEN")
    headers = {}
    if token:
        headers["Authorization"]=  "token %s" % token
    
    user = os.getenv("GH_USER")
    pw = os.getenv("GH_PW")
    auth = None
    if user and pw:
        auth = requests.auth.HTTPBasicAuth(user, pw)

    headers["Accept"] = "application/vnd.github.v3+json"

    owner = "conan-io"
    repo = "conan-center-index"

    prs = list()

    page = 1
    while True:
        r = requests.get(f"https://api.github.com/repos/{owner}/{repo}/pulls", headers=headers, auth=auth, params=
        {
            "state": "open",
            "sort": "created",
            "direction": "desc",
            "per_page": 100,
            "page": str(page)
        })
        r.raise_for_status()
        print("%s/%s github api call used, remaining %s until %s" % (r.headers["X-Ratelimit-Used"], r.headers["X-RateLimit-Limit"], r.headers["X-RateLimit-Remaining"], datetime.datetime.fromtimestamp(int(r.headers["X-Ratelimit-Reset"]))))
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

    for pr in prs:
        for lib in pr["libs"]:
            if not lib in libs:
                libs[lib] = list()
            libs[lib].append(pr["number"])
    

    msg = "The following table lists all the pull requests modifying files belonging to the same recipe.\n"
    msg += "| Library | Pull requests |\n"
    msg += "| --- | --- |\n"
    for l in libs:
        if len(libs[l]) > 1:
            msg += "| %s | " % l
            msg += ", ".join(["#%s" % pr for pr in libs[l]])
            msg += " |\n"
    print(msg)

    issue_number = os.getenv("GH_ISSUE_NUMBER")
    if issue_number:    
        print("updating issue")
        r = requests.patch(f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}", headers=headers, auth=auth, json=
        {
            "body": msg,
        })
        r.raise_for_status()
        print("%s/%s github api call used, remaining %s until %s" % (r.headers["X-Ratelimit-Used"], r.headers["X-RateLimit-Limit"], r.headers["X-RateLimit-Remaining"], datetime.datetime.fromtimestamp(int(r.headers["X-Ratelimit-Reset"]))))
                
        

if __name__ == "__main__":
    # execute only if run as a script
    main()
