#!/usr/bin/env python3
"""
One-time backfill: scores all existing github_projects rows in Supabase
using their stored data and writes rubric_score + rubric_tier back.

Usage:
  export SUPABASE_URL=https://...supabase.co
  export SUPABASE_SERVICE_KEY=eyJ...
  python backfill_rubric_scores.py
"""
import os
import sys
from datetime import datetime

try:
    from supabase import create_client
except ImportError:
    print("Run: pip install supabase")
    sys.exit(1)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ Set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars")
    sys.exit(1)

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def _score_github_project(r: dict) -> tuple:
    """Compute rubric score and tier using only stored Supabase fields.
    Mirrors the function in forge.py (OpenClaw Eval Rubric v1.3).
    Returns (score: int, tier: str).
    """
    stars         = r.get('stars', 0) or 0
    forks         = r.get('forks', 0) or 0
    lic           = r.get('license', '') or ''
    topics        = r.get('topics', []) or []
    desc          = (r.get('description', '') or '').lower()
    name          = (r.get('name', '') or '').lower()
    owner         = (r.get('owner', '') or '').lower()
    pushed_at     = r.get('pushed_at', '') or ''
    created_at    = r.get('created_at', '') or ''
    open_issues   = r.get('open_issues_count', 0) or 0
    archived      = r.get('archived', False) or False
    fork_ratio    = forks / max(stars, 1)
    today         = datetime.today().date()

    def _days_since(iso):
        if not iso: return 9999
        try: return (today - datetime.fromisoformat(iso[:10]).date()).days
        except: return 9999

    days_created     = _days_since(created_at)
    last_commit_days = _days_since(pushed_at)

    # ── AUTO-DISQUALIFIERS
    if lic in ('NOASSERTION', 'SSPL-1.0'):
        return 0, 'skip'
    for word in ('test', 'demo', 'temp', 'wip', 'todo', 'untitled'):
        if word in name:
            return 0, 'skip'
    if last_commit_days >= 548 and open_issues > 5:
        return 0, 'skip'

    # ── 1. ACTIVITY (0–30)
    if   last_commit_days <= 60:  act = 24
    elif last_commit_days <= 180: act = 17
    elif last_commit_days <= 365: act = 9
    else:                         act = 2
    if days_created <= 30: act = min(act, 15)

    # ── 2. QUALITY (0–25)
    qual = 12
    if   lic in ('MIT', 'Apache-2.0', 'BSD-2-Clause', 'BSD-3-Clause'): qual += 2
    elif not lic:                                                         qual -= 5
    elif lic in ('GPL-3.0', 'AGPL-3.0'):                                qual -= 2
    if stars > 5000 and lic in ('MIT', 'Apache-2.0'):                   qual += 2
    qual = max(0, min(25, qual))

    # ── 3. RELEVANCE (0–25)
    openclaw_kw = {'openclaw', 'clawdbot', 'moltbot', 'moltis', 'clawd',
                   'skills', 'skill', 'openclaw-skills', 'clawdbot-skill', 'crustacean'}
    topic_str = ' '.join(topics).lower()
    kw_hits   = sum(1 for k in openclaw_kw if k in topic_str)

    if   owner == 'openclaw' or name == 'openclaw':                          rel = 23
    elif any(k in name for k in ('awesome-openclaw', 'openclaw-skills',
                                  'openclaw-usecases')):                      rel = 20
    elif 'openclaw' in name or 'moltis' in name:                             rel = 18
    elif any(k in name for k in ('skill', 'awesome', 'usecases')):          rel = 16
    elif any(k in name for k in ('claw', 'molty', 'clawdbot', 'clawd')):    rel = 16
    elif kw_hits >= 3:                                                        rel = 15
    elif kw_hits >= 1:                                                        rel = 12
    elif 'openclaw' in desc or 'clawdbot' in desc or 'moltbot' in desc:     rel = 10
    else:                                                                     rel =  6
    if fork_ratio > 0.20: rel = min(25, rel + 2)

    # ── 4. TRACTION (0–15)
    if   stars >= 20000 and forks >= 2000:    trac = 13
    elif stars >= 5000  and forks >= 300:     trac = 10
    elif stars >= 1000  and forks >= 50:      trac = 7
    elif days_created <= 90 and stars >= 200: trac = 4
    else:                                      trac = 2
    if fork_ratio > 0.20:                     trac = min(15, trac + 2)
    if forks == 0 and stars > 500:            trac = max(0, trac - 3)

    # ── 5. NOVELTY (0–5)
    novelty_words = {'memory', 'mem', 'router', 'proxy', 'studio', 'lancedb',
                     'security', 'translation', 'guide', 'usecases', 'free'}
    if   owner == 'openclaw' or name == 'openclaw' or stars > 20000: novelty = 4
    elif any(k in name for k in novelty_words):                       novelty = 4
    elif stars > 5000 or 'awesome' in name:                           novelty = 3
    else:                                                              novelty = 2

    total = act + qual + rel + trac + novelty
    if archived and total >= 75: total = 74

    if   total >= 75: tier = 'featured'
    elif total >= 50: tier = 'listed'
    elif total >= 25: tier = 'watchlist'
    else:             tier = 'skip'
    return total, tier


def main():
    print("🔍 Fetching all github_projects from Supabase…")
    all_rows = []
    page_size = 1000
    offset = 0
    while True:
        resp = sb.table('github_projects').select('*').range(offset, offset + page_size - 1).execute()
        rows = resp.data or []
        all_rows.extend(rows)
        print(f"  Fetched {len(all_rows)} rows total")
        if len(rows) < page_size:
            break
        offset += page_size

    print(f"📊 Scoring {len(all_rows)} repos…")
    updates = []
    for r in all_rows:
        score, tier = _score_github_project(r)
        updates.append({'url': r['url'], 'rubric_score': score, 'rubric_tier': tier})

    print("💾 Upserting scores in batches of 200…")
    batch_size = 200
    for i in range(0, len(updates), batch_size):
        batch = updates[i:i + batch_size]
        sb.table('github_projects').upsert(batch).execute()
        print(f"  Batch {i // batch_size + 1}: {len(batch)} rows written")

    tiers: dict = {}
    for u in updates:
        t = u['rubric_tier']
        tiers[t] = tiers.get(t, 0) + 1
    print("✅ Done. Tier breakdown:", tiers)


if __name__ == '__main__':
    main()
