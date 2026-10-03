#!/usr/bin/env python3
"""Generate GitHub profile stat cards as SVG files.

Environment:
  GH_USER      GitHub username to report on (required)
  STATS_TOKEN  Classic token with no scopes: read-only access to public data (required)
  ORG_NAME     Organization whose private pull requests should be counted (optional)
  ORG_TOKEN    Read-only fine-grained token owned by ORG_NAME (optional)
"""

import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from html import escape
from pathlib import Path

API = "https://api.github.com"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"

THEMES = {
    "dark": {"bg": "#0d1117", "border": "#30363d", "title": "#e6edf3", "text": "#8b949e", "accent": "#58a6ff"},
    "light": {"bg": "#ffffff", "border": "#d0d7de", "title": "#1f2328", "text": "#59636e", "accent": "#0969da"},
}


def request(url, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "profile-stats",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def graphql(token, query, variables=None):
    body = request(f"{API}/graphql", token, {"query": query, "variables": variables or {}})
    if body.get("errors"):
        raise RuntimeError(f"GraphQL error: {body['errors']}")
    return body["data"]


def search_count(token, query):
    url = f"{API}/search/issues?" + urllib.parse.urlencode({"q": query, "per_page": 1})
    return request(url, token)["total_count"]


PROFILE_QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionYears
      contributionCalendar { totalContributions }
    }
    repositories(ownerAffiliations: OWNER, isFork: false, first: 100,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""


def yearly_query(years):
    blocks = "\n".join(
        f'y{y}: contributionsCollection(from: "{y}-01-01T00:00:00Z", to: "{y}-12-31T23:59:59Z") {{'
        " contributionCalendar { totalContributions }"
        " totalCommitContributions restrictedContributionsCount }"
        for y in years
    )
    return f"query($login: String!) {{ user(login: $login) {{ {blocks} }} }}"


def collect(user, token, org, org_token):
    profile = graphql(token, PROFILE_QUERY, {"login": user})["user"]
    years = profile["contributionsCollection"]["contributionYears"]
    yearly = graphql(token, yearly_query(years), {"login": user})["user"]

    total = sum(yearly[f"y{y}"]["contributionCalendar"]["totalContributions"] for y in years)
    private = sum(yearly[f"y{y}"]["restrictedContributionsCount"] for y in years)
    commits = sum(yearly[f"y{y}"]["totalCommitContributions"] for y in years)

    # Pull requests are counted with search. The personal token sees public work;
    # the organization token adds that organization's private repositories.
    exclude_org = f" -org:{org}" if org and org_token else ""
    merged = search_count(token, f"is:pr is:merged author:{user}{exclude_org}")
    reviewed = search_count(token, f"is:pr reviewed-by:{user} -author:{user}{exclude_org}")
    if org and org_token:
        try:
            merged += search_count(org_token, f"is:pr is:merged author:{user} org:{org}")
            reviewed += search_count(org_token, f"is:pr reviewed-by:{user} -author:{user} org:{org}")
        except urllib.error.HTTPError as err:
            print(f"warning: organization search failed ({err.code}); using public counts only", file=sys.stderr)

    languages = {}
    for repo in profile["repositories"]["nodes"]:
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            entry = languages.setdefault(name, {"size": 0, "color": edge["node"]["color"] or "#8b949e"})
            entry["size"] += edge["size"]

    return {
        "rows": [
            ("Total contributions", total),
            ("Contributions, last 12 months", profile["contributionsCollection"]["contributionCalendar"]["totalContributions"]),
            ("Private contributions", private),
            ("Pull requests merged", merged),
            ("Pull requests reviewed", reviewed),
            ("Public commits", commits),
        ],
        "languages": languages,
        "since": min(years) if years else datetime.date.today().year,
    }


def card(width, height, theme, title, body):
    t = THEMES[theme]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="6" '
        f'fill="{t["bg"]}" stroke="{t["border"]}"/>'
        f'<g font-family="{FONT}">'
        f'<text x="25" y="38" font-size="16" font-weight="600" fill="{t["title"]}">{escape(title)}</text>'
        f"{body}</g></svg>"
    )


def stats_svg(data, theme):
    t = THEMES[theme]
    width, top, step = 495, 72, 30
    body = []
    for i, (label, value) in enumerate(data["rows"]):
        y = top + i * step
        if i:
            body.append(f'<line x1="25" x2="{width - 25}" y1="{y - 20}" y2="{y - 20}" stroke="{t["border"]}" stroke-width="1"/>')
        body.append(f'<circle cx="29" cy="{y - 4.5}" r="3" fill="{t["accent"]}"/>')
        body.append(f'<text x="42" y="{y}" font-size="13" fill="{t["text"]}">{escape(label)}</text>')
        body.append(f'<text x="{width - 25}" y="{y}" font-size="13" font-weight="600" '
                    f'text-anchor="end" fill="{t["title"]}">{value:,}</text>')
    footer_y = top + len(data["rows"]) * step - 2
    body.append(f'<text x="25" y="{footer_y}" font-size="11" fill="{t["text"]}">'
                f'Since {data["since"]} · Updated {datetime.date.today():%d %b %Y}</text>')
    return card(width, footer_y + 20, theme, "GitHub Activity", "".join(body))


def languages_svg(data, theme, limit=6):
    t = THEMES[theme]
    width, bar_w = 495, 445
    ranked = sorted(data["languages"].items(), key=lambda kv: kv[1]["size"], reverse=True)[:limit]
    total = sum(v["size"] for _, v in ranked) or 1

    body = ['<clipPath id="bar"><rect x="25" y="56" width="445" height="8" rx="4"/></clipPath>',
            f'<rect x="25" y="56" width="{bar_w}" height="8" rx="4" fill="{t["border"]}"/><g clip-path="url(#bar)">']
    x = 25.0
    for _, v in ranked:
        w = bar_w * v["size"] / total
        body.append(f'<rect x="{x:.2f}" y="56" width="{w:.2f}" height="8" fill="{v["color"]}"/>')
        x += w
    body.append("</g>")

    for i, (name, v) in enumerate(ranked):
        col, row = i % 2, i // 2
        cx, cy = 25 + col * 230, 92 + row * 26
        pct = 100 * v["size"] / total
        body.append(f'<circle cx="{cx + 5}" cy="{cy - 4}" r="5" fill="{v["color"]}"/>')
        body.append(f'<text x="{cx + 17}" y="{cy}" font-size="13" fill="{t["title"]}">{escape(name)}'
                    f'<tspan fill="{t["text"]}" dx="6">{pct:.1f}%</tspan></text>')
    rows = (len(ranked) + 1) // 2
    return card(width, 92 + rows * 26, theme, "Most Used Languages", "".join(body))


def main():
    user = os.environ["GH_USER"]
    token = os.environ["STATS_TOKEN"]
    org = os.environ.get("ORG_NAME", "").strip()
    org_token = os.environ.get("ORG_TOKEN", "").strip()
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    out.mkdir(parents=True, exist_ok=True)

    data = collect(user, token, org, org_token)
    for theme in THEMES:
        (out / f"stats-{theme}.svg").write_text(stats_svg(data, theme))
        (out / f"languages-{theme}.svg").write_text(languages_svg(data, theme))
    for label, value in data["rows"]:
        print(f"{label}: {value:,}")


if __name__ == "__main__":
    main()
