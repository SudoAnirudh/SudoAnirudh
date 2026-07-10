#!/usr/bin/env python3
"""
gitstats-terminal
------------------
Pulls repos / commits / stars / followers / lines-of-code / contribution
streak for a GitHub user and renders it into a terminal-styled SVG card.

Env vars required:
    ACCESS_TOKEN   - GitHub PAT with `repo` (read) + `read:user` scopes
    GH_USERNAME    - GitHub username to report on

Run:
    python main.py
Outputs:
    card.svg        (in repo root, or OUTPUT_PATH env var)
"""

import os
import json
import time
import datetime as dt
from pathlib import Path

import requests

GITHUB_API = "https://api.github.com/graphql"
REST_API = "https://api.github.com"

TOKEN = os.environ.get("ACCESS_TOKEN", "")
USERNAME = os.environ.get("GH_USERNAME", "")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "card.svg")
CACHE_PATH = Path(os.environ.get("CACHE_PATH", "cache.json"))

HEADERS = {"Authorization": f"bearer {TOKEN}"}
REST_HEADERS = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json"}


# --------------------------------------------------------------------------- #
# GraphQL helpers
# --------------------------------------------------------------------------- #

def gql(query: str, variables: dict) -> dict:
    resp = requests.post(
        GITHUB_API, json={"query": query, "variables": variables}, headers=HEADERS, timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


USER_QUERY = """
query($login: String!) {
  user(login: $login) {
    name
    login
    createdAt
    followers { totalCount }
    repositories(first: 100, ownerAffiliations: [OWNER], isFork: false, privacy: PUBLIC) {
      totalCount
      nodes {
        name
        nameWithOwner
        stargazerCount
        pushedAt
        isArchived
      }
    }
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            date
            contributionCount
          }
        }
      }
    }
  }
}
"""


def fetch_user_block(username: str) -> dict:
    return gql(USER_QUERY, {"login": username})["user"]


# --------------------------------------------------------------------------- #
# Streak calculation from the contribution calendar
# --------------------------------------------------------------------------- #

def compute_streaks(calendar: dict) -> dict:
    days = []
    for week in calendar["weeks"]:
        for d in week["contributionDays"]:
            days.append((d["date"], d["contributionCount"]))
    days.sort(key=lambda x: x[0])

    today = dt.date.today().isoformat()

    # current streak: walk backwards from most recent day that has data
    current = 0
    longest = 0
    running = 0
    last_contribution_date = None

    for date_str, count in days:
        if count > 0:
            running += 1
            longest = max(longest, running)
            last_contribution_date = date_str
        else:
            running = 0

    # recompute current streak by walking from the end backwards
    for date_str, count in reversed(days):
        if date_str == today and count == 0:
            # today hasn't happened yet / no commits yet today - don't break the streak
            continue
        if count > 0:
            current += 1
        else:
            break

    return {
        "current_streak": current,
        "longest_streak": longest,
        "last_contribution_date": last_contribution_date,
        "total_last_year": sum(c for _, c in days),
        "daily": days,  # for the waveform
    }


# --------------------------------------------------------------------------- #
# Lines of code via the contributor-stats REST endpoint
# (much cheaper than walking every commit: GitHub precomputes this weekly)
# --------------------------------------------------------------------------- #

def fetch_repo_loc(owner_repo: str, username: str, cache: dict) -> dict:
    cached = cache.get(owner_repo)
    if cached and cached.get("checked_at"):
        checked = dt.datetime.fromisoformat(cached["checked_at"])
        if dt.datetime.utcnow() - checked < dt.timedelta(hours=12):
            print(" (cached)", end="", flush=True)
            return {"additions": cached["additions"], "deletions": cached["deletions"]}

    url = f"{REST_API}/repos/{owner_repo}/stats/contributors"
    for attempt in range(6):
        resp = requests.get(url, headers=REST_HEADERS, timeout=30)
        if resp.status_code == 202:
            print(".", end="", flush=True)
            time.sleep(2)
            continue
        resp.raise_for_status()
        break
    else:
        print(" (failed/timeout)", end="", flush=True)
        return {"additions": 0, "deletions": 0}

    additions, deletions = 0, 0
    try:
        for contributor in resp.json():
            if contributor.get("author") and contributor["author"].get("login") == username:
                for week in contributor.get("weeks", []):
                    additions += week.get("a", 0)
                    deletions += week.get("d", 0)
                break
    except (ValueError, TypeError):
        pass

    cache[owner_repo] = {
        "additions": additions,
        "deletions": deletions,
        "checked_at": dt.datetime.utcnow().isoformat(),
    }
    return {"additions": additions, "deletions": deletions}


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache, indent=2))


# --------------------------------------------------------------------------- #
# SVG rendering
# --------------------------------------------------------------------------- #

def build_waveform_bars(daily: list, bar_count: int = 53) -> str:
    """Collapse the last `bar_count` weeks of daily contributions into one
    bar per week (sum of that week), scaled to a max bar height, styled as
    a terminal equalizer / waveform."""
    weeks = []
    bucket = []
    for i, (_, count) in enumerate(daily):
        bucket.append(count)
        if len(bucket) == 7:
            weeks.append(sum(bucket))
            bucket = []
    if bucket:
        weeks.append(sum(bucket))

    weeks = weeks[-bar_count:]
    if not weeks:
        weeks = [0] * bar_count
    max_val = max(weeks) or 1

    bar_w = 6
    gap = 2
    max_h = 34
    base_y = 40

    bars = []
    for i, val in enumerate(weeks):
        h = 2 if val == 0 else max(3, round((val / max_val) * max_h))
        x = i * (bar_w + gap)
        y = base_y - h
        intensity = val / max_val
        if intensity == 0:
            color = "#292e42"
        elif intensity < 0.34:
            color = "#3d59a1"
        elif intensity < 0.67:
            color = "#7aa2f7"
        else:
            color = "#9ece6a"
        delay = round(i * 0.02, 2)
        bars.append(
            f'<rect x="{x}" y="{y}" width="{bar_w}" height="{h}" rx="1.5" fill="{color}">'
            f'<animate attributeName="opacity" values="0;1" dur="0.4s" begin="{delay}s" fill="freeze"/>'
            f"</rect>"
        )
    return "".join(bars)


def esc(s) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_svg(template_path: Path, output_path: Path, values: dict) -> None:
    svg = template_path.read_text()
    for key, val in values.items():
        svg = svg.replace(f"{{{{{key}}}}}", str(val))
    output_path.write_text(svg)


def fmt(n: int) -> str:
    return f"{n:,}"


def main():
    if not TOKEN or not USERNAME:
        raise SystemExit("ACCESS_TOKEN and GH_USERNAME environment variables are required.")

    print(f"Loading cache...")
    cache = load_cache()

    print(f"Fetching user details for {USERNAME}...")
    user = fetch_user_block(USERNAME)
    repos = [r for r in user["repositories"]["nodes"] if not r["isArchived"]]
    total_repos = user["repositories"]["totalCount"]
    followers = user["followers"]["totalCount"]
    stars = sum(r["stargazerCount"] for r in repos)

    calendar = user["contributionsCollection"]["contributionCalendar"]
    streaks = compute_streaks(calendar)

    print(f"Calculating Lines of Code (LOC) for {len(repos)} public repos...")
    total_add, total_del = 0, 0
    for idx, r in enumerate(repos, 1):
        print(f"  [{idx}/{len(repos)}] {r['nameWithOwner']}...", end="", flush=True)
        loc = fetch_repo_loc(r["nameWithOwner"], USERNAME, cache)
        print(f" Done (+{loc['additions']}/-{loc['deletions']})")
        total_add += loc["additions"]
        total_del += loc["deletions"]

    print("Saving cache...")
    save_cache(cache)

    account_age_days = (
        dt.date.today() - dt.date.fromisoformat(user["createdAt"][:10])
    ).days
    account_age_years = round(account_age_days / 365.25, 1)

    waveform = build_waveform_bars(streaks["daily"])

    def prefix(label: str, width: int = 16) -> str:
        dots = max(1, width - len(label))
        return esc(label) + ("." * dots)

    values = {
        "USERNAME": esc(user.get("name") or user["login"]),
        "HANDLE": esc(user["login"]),
        "REPOS": fmt(total_repos),
        "REPOS_PREFIX": prefix("repos"),
        "COMMITS_YEAR": fmt(streaks["total_last_year"]),
        "COMMITS_PREFIX": prefix("commits (1y)"),
        "STARS": fmt(stars),
        "STARS_PREFIX": prefix("stars"),
        "FOLLOWERS": fmt(followers),
        "FOLLOWERS_PREFIX": prefix("followers"),
        "LOC_ADD": fmt(total_add),
        "LOC_ADD_PREFIX": prefix("loc++"),
        "LOC_DEL": fmt(total_del),
        "LOC_DEL_PREFIX": prefix("loc--"),
        "CURRENT_STREAK": streaks["current_streak"],
        "LONGEST_STREAK": streaks["longest_streak"],
        "LAST_COMMIT": streaks["last_contribution_date"] or "n/a",
        "ACCOUNT_AGE": account_age_years,
        "WAVEFORM_BARS": waveform,
        "GENERATED_AT": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }

    render_svg(Path("templates/terminal_card.svg"), Path(OUTPUT_PATH), values)
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
