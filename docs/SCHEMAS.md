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

**Post mode (`--post`)** uses `harvestapi~linkedin-post-reactions` and `harvestapi~linkedin-post-comments` with `{"posts": [url], "maxItems": n, "profileScraperMode": "main"}` (`"short"` is the actor default and returns the opaque `actor{}` above). In `main` mode `actor{}` is a full profile: `id`, `name`, `firstName`, `lastName`, `linkedinUrl` (vanity), `publicIdentifier`, `headline`, `location`, `currentPosition[]`, `experience[]`, `followerCount`, `connectionsCount`, `industry`, `about`, `education`, `emails`. `position` may be absent; the harvester falls back to `headline`. Price per event: `post-reaction` / `post-comment` $0.002, `main-profile` +$0.002 per engager (`full-profile` $0.004 and `full-profile-with-email` $0.01 are not used).

---

## 1b. Opaque ID resolution - `harvestapi/linkedin-profile-scraper`

Used by `src/resolve.py`. Price: $0.004 per `profile` event. On a free Apify plan the actor is limited to 10 items per run; above that it returns a single `{"error": "Free users are limited to 10 items per run. ..."}` item with run status `SUCCEEDED`.

**Input** (provide at least one of `urls` / `publicIdentifiers` / `profileIds`):

```json
{"profileIds": ["ACoAA..."], "profileScraperMode": "Profile details no email ($4 per 1k)"}
```

The mode enum values are literal strings; the other is `"Profile details + email search ($10 per 1k)"`.

**Output** (observed 2026-10-19, one item per resolved profile):

```
id                 the ACoAA... id that was sent
originalQuery      {"profileId": "ACoAA..."}
publicIdentifier   vanity slug, e.g. "javeriashah"
linkedinUrl        https://www.linkedin.com/in/<publicIdentifier>
firstName, lastName, headline
location           {"linkedinText": "London, England, United Kingdom", "countryCode": "GB",
                    "parsed": {"text", "countryCode", "country", "countryFull", "state", "city", "regionCode"}}
currentPosition[]  {position, companyName, companyUniversalName, companyLinkedinUrl, companyId, location,
                    employmentType, workplaceType, duration, description, skills, startDate, endDate, companyLogo}
experience[]       same shape as currentPosition
```

Also present: `about`, `causes`, `certifications`, `composeOptionType`, `connectionsCount`, `courses`, `coverPicture`, `creator`, `education`, `emails`, `featured`, `followerCount`, `hiring`, `honorsAndAwards`, `industry`, `influencer`, `interests`, `languages`, `memorialized`, `moreProfiles`, `multiLocaleHeadline`, `objectUrn`, `openToWork`, `organizations`, `patents`, `photo`, `premium`, `primaryLocale`, `profileActions`, `profileLocales`, `profilePicture`, `profileTopEducation`, `projects`, `publications`, `receivedRecommendations`, `registeredAt`, `sectionTotals`, `services`, `skills`, `topSkills`, `verified`, `volunteering`, `websites` (optional).

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
| `qualification_threshold`, `qualification_status` | 1-5; qualified/rejected | Required fit score and whether this person meets it |
| `qualification_reason` | text, required | Evidence supporting the fit score, ICP mismatches and important missing facts; required for accepted and rejected people |
| `intent` | high/medium/low | Derived from comment text; reaction weight if no comment |
| `intent_reason` | text, required | Comment supporting intent, or the limitation of reaction-only engagement |
| `rank_score` | float | `icp_fit * (1 + ln(engagement_count)) * intent_weight` |

Both reason fields are required in automatic scorer responses and in-session `scores.json` entries. The ledger migrates existing score tables to store them; reasonless scores require a new judgment. The gate adds `eligibility_reason` for suppression, campaign history and score freshness. The optional Activity Intel stage adds `activity_reason` (also `outreach_reason` for compatibility), `activity_model`, status, probability and evidence link. Its default model is Jev (`jev-latest`) through TypeSafe. Skipped checks are `not_checked`; failed judgments are `unknown`, with reasons rather than an inactivity claim.

**Intent weights:** `high=1.0`, `medium=0.6`, `low=0.3`

**Reaction type to intent mapping:**

| Reaction | Intent |
|----------|--------|
| INTEREST, PRAISE, APPRECIATION | low |
| LIKE, EMPATHY, ENTERTAINMENT | low |
