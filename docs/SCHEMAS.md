# Actor Schemas

Reference for the Apify actors used by this tool.

---

## 1. Posts + Engagers - `harvestapi~linkedin-profile-posts`

Scrapes posts, reactions, and comments for a LinkedIn profile in a single run. No separate engager actor needed.

**Pricing:** PRICE_PER_DATASET_ITEM = ~$0.00175/item. A run pulling 8 posts with 100 reactions + 50 comments each returns ~1,200 items = ~$2.10.

**Input:**

```json
{
  "targetUrls": ["https://www.linkedin.com/in/<slug>"],
  "maxPosts": 0,
  "postedLimit": "month",
  "scrapeReactions": true,
  "maxReactions": 100,
  "scrapeComments": true,
  "maxComments": 50,
  "commentsPostedLimit": "month",
  "includeReposts": true,
  "includeQuotePosts": true
}
```

| Field | Type | Notes |
|-------|------|-------|
| `targetUrls` | string[] | One profile URL per run |
| `maxPosts` | int | `0` = all available |
| `postedLimit` | string | `"24h"` / `"week"` / `"month"` |
| `maxReactions` | int | Default 5; raise for larger engager pool |
| `maxComments` | int | Default 5; comments carry more intent signal |

**Output:** Flat dataset. Discriminate on `type`:

| `type` | Meaning |
|--------|---------|
| `post` | The profile's post. Has `postId`, content, engagement counts. |
| `reaction` | An engager who reacted. Has `reactionType` and `actor{}`. |
| `comment` | An engager who commented. Has `commentary` (text) and `actor{}`. |

**`actor{}` object (present on reaction and comment items):**

```
actor.id          fsd_profile ID (e.g. ACoAA...)
actor.name        display name
actor.linkedinUrl vanity URL from comments; opaque /in/ACoAA... from reactions
actor.position    headline string, often "<role> @<company>"
actor.pictureUrl  avatar URL
```

**Engager extraction logic:** Filter items to `type in (reaction, comment)`. Key by `actor.id`. Deduplicate. Prefer vanity URLs from comments over opaque reaction URLs. Attach comment text when present.

**Accessing results via REST:**

```
POST  /v2/acts/harvestapi~linkedin-profile-posts/runs?token=<APIFY_TOKEN>
      Body: input JSON above

GET   /v2/actor-runs/<run-id>?token=<APIFY_TOKEN>
      Poll until status == "SUCCEEDED"
      Response carries: defaultDatasetId, usageTotalUsd

GET   /v2/datasets/<dataset-id>/items?token=<APIFY_TOKEN>&format=json
      Returns the flat item array
```

---

## 2. Company enrich - `harvestapi~linkedin-company`

Bulk company scraper. Use with `--enrich` flag to pull industry, employee count, and location for engager companies.

**Input:** company URLs or names. **Output:** company profile objects.

Exact per-item pricing varies; measure on first run via `usageTotalUsd` in the run response.

---

## 3. Ranking (OpenAI, not Apify)

`src/rank.py` calls `gpt-4.1-mini` to score engagers. Not an Apify actor.

**Model:** `gpt-4.1-mini`  
**Batch size:** 25 engagers per call  
**Cost:** ~$0.40/M input + $1.60/M output tokens. Typical run: negligible (<$0.10)

**Scores per engager:**

| Field | Range | Description |
|-------|-------|-------------|
| `icp_fit` | 1-5 | Semantic match of position headline vs. ICP titles/industries |
| `intent` | high/medium/low | Derived from comment text; reaction weight if no comment |
| `rank_score` | float | `icp_fit * (1 + ln(engagement_count)) * intent_weight` |

**Intent weights:** `high=1.0`, `medium=0.6`, `low=0.3`

**Reaction type to intent mapping:**

| Reaction | Intent |
|----------|--------|
| INTEREST, PRAISE, APPRECIATION | low |
| LIKE, EMPATHY, ENTERTAINMENT | low |
