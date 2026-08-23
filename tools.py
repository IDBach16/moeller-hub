"""
tools.py -- the existing analytics tools, as data rather than as HTML.

Roadmap section 1 is explicit that the current tools are not rebuilt: they become
the infrastructure underneath a player-centred experience. Holding them as a list
here (instead of eight hand-written <div>s in a template) is what lets Phase C
move them from the front page to /tools without touching any markup.

`category` is the nav section each tool belongs to once that split happens --
see PLAYER_DEV_SPEC.md section 8.1.
"""

TOOLS = [
    {
        "key": "rapsodo",
        "title": "Moeller Rapsodo",
        "category": "players",
        "desc": "Bullpen pitch quality from the Rapsodo unit -- location, movement, "
                "arm slot, and same-level percentile rankings. Updates itself every "
                "morning from the nightly pull.",
        "url": "https://rapsodo-app-production.up.railway.app/",
        "icon": '<circle cx="12" cy="12" r="10"/>'
                '<path d="M7 15c2-5 8-5 10-9"/>'
                '<circle cx="8.5" cy="9" r="1.2"/><circle cx="15.5" cy="15" r="1.2"/>',
    },
    {
        "key": "scouting",
        "title": "Scouting Agent",
        "category": "prep",
        "desc": "Our scouting tool that allows you to ask questions and get "
                "information from the data we have collected.",
        "url": "https://web-production-510f.up.railway.app/",
        "icon": '<circle cx="12" cy="12" r="10"/>'
                '<path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/>'
                '<line x1="2" y1="12" x2="22" y2="12"/>',
    },
    {
        "key": "pitcher_cards",
        "title": "Pitcher Cards",
        "category": "players",
        "desc": "Pitcher information and updates for quick reference before and during games.",
        "url": "https://web-production-08767.up.railway.app/",
        "icon": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>'
                '<path d="M8 2s1.5 2 4 2 4-2 4-2"/>',
    },
    {
        "key": "hitter_cards",
        "title": "Hitter Cards",
        "category": "players",
        "desc": "Hitter information and updates that can be used for player evaluation, "
                "planning, and in-game reference.",
        "url": "https://web-production-51eb5b.up.railway.app/",
        "icon": '<path d="M4 20h16"/><path d="M4 20V10l4-6h8l4 6v10"/>'
                '<rect x="8" y="12" width="8" height="8" rx="1"/>'
                '<line x1="12" y1="12" x2="12" y2="8"/>',
    },
    {
        "key": "umpire_cards",
        "title": "Umpire Cards",
        "category": "prep",
        "desc": "Quick reference tool for umpire information and game-use situations.",
        "url": "https://web-production-196103.up.railway.app/",
        "icon": '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>'
                '<circle cx="12" cy="12" r="3"/>',
    },
    {
        "key": "team_stats",
        "title": "Team Stats",
        "category": "prep",
        "desc": "Full team batting and pitching stats dashboard with leaderboards "
                "and Synergy scouting.",
        "note": "Login: <strong>moeller</strong> &nbsp;|&nbsp; Password: <strong>moeller1</strong>",
        "url": "https://moeller-2026-stats-production.up.railway.app/login",
        "icon": '<path d="M18 20V10M12 20V4M6 20v-6"/>',
    },
    {
        "key": "video_search",
        "title": "AWRE Video Search",
        "category": "video",
        "desc": "Search game video by team, player, pitch type, and result. Filter "
                "9,600+ pitches across 45 games with multi-angle playback.",
        "url": "https://web-production-12b79.up.railway.app/",
        "icon": '<polygon points="5 3 19 12 5 21 5 3"/>',
    },
    {
        "key": "charting",
        "title": "Charting App",
        "category": "collect",
        "desc": "Pitch-by-pitch charting for off-season bullpens and live ABs. Tap the "
                "attack zone, log the pitch, and the coach dashboard updates behind it.",
        "url": "https://moeller-charting-production.up.railway.app/",
        "icon": '<circle cx="12" cy="12" r="9"/><path d="M8.5 4.5c2 3 2 12 0 15"/>'
                '<path d="M15.5 4.5c-2 3-2 12 0 15"/>',
    },
    {
        "key": "pitch_overlays",
        "title": "Pitch Overlays",
        "category": "video",
        "desc": "Delivery overlay comparisons by pitcher. Side-by-side and stacked views "
                "synced to release point for mechanical analysis.",
        "url": "https://drive.google.com/drive/folders/"
               "1gruNdqaNpmhgRp2_4qdidSIRP12vnfkh?usp=sharing",
        "icon": '<rect x="2" y="3" width="20" height="14" rx="2"/>'
                '<line x1="8" y1="21" x2="16" y2="21"/>'
                '<line x1="12" y1="17" x2="12" y2="21"/>'
                '<path d="M7 10l3 3 7-7" stroke-width="2"/>',
    },
]

# The Coach Assistant's example questions.
CHIPS = [
    "Who led the team in AVG in 2026?",
    "What was Jack Ujvagi’s best pitch?",
    "How do our off-season bullpens look?",
    "Where do I see Maybury’s Rapsodo work?",
]


def by_category(category):
    return [t for t in TOOLS if t.get("category") == category]
