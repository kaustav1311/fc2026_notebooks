# Comprehensive Metrics Audit: Sources, Types & Usage

## Executive Summary
| Category | Source | Used | Available | Gap |
|----------|--------|------|-----------|-----|
| **Team Stats** | FotMob | 15 metrics | ~20+ | Advanced possession/pressing |
| **Player Stats** | FotMob | 25+ metrics | 40+ | Match-level detailed stats |
| **Match Data** | ESPN/SofaScore | 5 basic metrics | 15+ | Live timeline, formations |
| **Form/Results** | FotMob/ESPN/SofaScore | Basic (W/D/L) | Extended form | Streak analytics |

---

## 1. TEAM METRICS

### ✅ CURRENTLY USED

#### FotMob - WC2026 Tournament Stats
**Source:** `getTeamRows()` from `data.fotmob.com/stats/77/season/24254/{statKey}.json`

| Metric | Type | Used In | Format |
|--------|------|---------|--------|
| goals | number | Team Stats Table | integer |
| goal_assist | number | Team Stats Table | integer |
| shots | number | Team Stats Table | integer |
| shots_on_target | number | Team Stats Table | integer |
| big_chance_created | number | Team Stats Table | integer |
| big_chance_missed | number | Team Stats Table | derived ratio |
| rating (avg) | float | Team Stats Table | 2 decimals |
| passes | number | Team Stats Table | integer |
| pass_success | percentage | Team Stats Table | % format |
| tackle | number | Team Stats Table | integer |
| defensive_actions | number | Team Stats Table | integer |
| fouls | number | Team Stats Table | integer |
| saves (GK) | number | Team Stats Table | integer |
| saves_per_90 (GK) | float | Team Stats Table | 2 decimals |
| duels_won | number | Team Stats Table | integer |

#### FotMob - Team Form (Recent Results)
**Source:** `getTeamWcForm()` from `/api/data/teams?id={teamId}`

| Metric | Type | Used In | Format |
|--------|------|---------|--------|
| result (W/D/L) | enum | Group Table Form | letter badge |
| date | ISO date | Group Table Form | visual indicator |

### 🟡 AVAILABLE BUT UNUSED

**From FotMob Stats Endpoints:**
- `pass_completion` (already have pass_success)
- `interceptions` (defensive stat)
- `clearances` (defensive stat)
- `aerial_duels_won` (% of aerials won)
- `corner_kicks_for` (set pieces)
- `corner_kicks_against` (conceded)
- `free_kicks_for` (set pieces)
- `offsides` (discipline)
- `possession` (% of ball)
- `shots_against` (defense metric)
- `expected_goals` (xG - possession quality)
- `expected_goals_against` (xGA)
- `penalty_kicks_scored` (set piece success)
- `penalty_kicks_missed` (conversion rate)
- `cards_yellow` (discipline)
- `cards_red` (discipline)

**From FotMob Team Overview API:**
- Squad formation (4-2-3-1, 3-5-2, etc.)
- Manager name
- Stadium info
- Recent injury list
- Team captain

---

## 2. PLAYER METRICS

### ✅ CURRENTLY USED

#### Player Profile (Static)
**Source:** `getPlayerDetail()` from `/api/data/playerData?id={playerId}`

| Metric | Type | Used In | Format |
|--------|------|---------|--------|
| name | string | Header | display name |
| position | string | Header + grid | e.g. "Striker" |
| otherPositions | string[] | Section | list of positions |
| height | string | Profile grid | "179 cm" |
| age | string | Profile grid | "24 years (3 Jul 2001)" |
| preferredFoot | string | Profile grid | "Right" / "Left" / "Both" |
| marketValue | string | Profile grid | "€23.8m" |
| countryCode | string | Header | 3-letter code |
| shirtNumber | string | Card | integer |
| contractEnd | string | Stored | not displayed |

#### WC2026 Tournament Stats (Detailed)
**Source:** FotMob playerData → statsSection

| Metric | Type | Used In | Outfield | GK | Format |
|--------|------|---------|----------|----|---------| 
| minutes | string | Tournament grid | ✓ | ✓ | integer |
| matchesPlayed | string | Tournament grid | ✓ | ✓ | integer |
| goals | string | Tournament grid | ✓ | - | integer |
| xg | string | Tournament grid | ✓ | - | 2 decimals |
| goalsPlusAssists | string | Tournament grid | ✓ | - | integer |
| shots | string | Tournament grid | ✓ | - | integer |
| shotsOnTarget | string | Tournament grid | ✓ | - | integer |
| passAccuracy | string | Tournament grid | ✓ | ✓ | percent |
| chancesCreated | string | Tournament grid | ✓ | - | integer |
| touchesOppBox | string | Tournament grid | ✓ | - | integer |
| defensiveContributions | string | Tournament grid | ✓ | - | integer |
| cleanSheets | string | Tournament grid | ✓ | ✓ | integer |
| yellowCards | string | Tournament grid | ✓ | ✓ | integer |
| redCards | string | Tournament grid | ✓ | ✓ | integer |
| savesTotal | string | Tournament grid | - | ✓ | integer |
| xgConceded | string | Tournament grid | - | ✓ | 2 decimals |

#### WC2026 Tournament Stats (Lite - Short Minutes)
**Source:** FotMob playerData → topStatCard (fallback)

| Metric | Type | Used In | Format |
|--------|------|---------|--------|
| goals | string | Lite grid | integer |
| minutes | string | Lite grid | integer |
| goalsPlusAssists | string | Lite grid | integer |
| rating | string | Lite grid | 2 decimals |
| shots | string | Lite grid | integer |

#### International Career Totals
**Source:** FotMob playerData → aggregated from seasonEntry

| Metric | Type | Used In | Format |
|--------|------|---------|--------|
| caps | number | Profile grid | integer |
| goals | number | Profile grid | integer |
| cleanSheets (GK) | number | Profile grid | integer |
| saves (GK) | number | Profile grid | integer |

#### Last Season (Club)
**Source:** FotMob playerData → mostRecentSeasonEntry

| Metric | Type | Used In | Format |
|--------|------|---------|--------|
| teamName | string | Last Season header | display |
| seasonLabel | string | Last Season header | "2025/2026" |
| appearances | number | Last Season tile | integer |
| goals | number | Last Season tile | integer |
| assists | number | Last Season tile | integer |
| rating | number | Last Season tile | 2 decimals |

#### Career History (Senior + National Teams)
**Source:** FotMob playerData → seniorSeasons + nationalSeasons

| Metric | Type | Used In | Format |
|--------|------|---------|--------|
| seasonLabel | string | Career table | "2025/2026" |
| teamName | string | Career table | display |
| teamLogoUrl | string | Career table | image |
| appearances | number | Career table | integer |
| goals | number | Career table | integer |
| assists | number | Career table | integer |
| rating | number | Career table | 2 decimals |
| isYouth | boolean | Career table | label |

#### Recent Matches (Form - Last 10)
**Source:** FotMob playerData → recentMatches + fallback to besoccer

| Metric | Type | Used In | Format |
|--------|------|---------|--------|
| date | ISO date | Form row | date pill |
| leagueName | string | Form row | text label |
| leagueLogoUrl | string | Form row | 16×16 image |
| homeCode | string | Form row | 3-letter |
| awayCode | string | Form row | 3-letter |
| homeBadgeUrl | string | Form row | 16×16 image |
| awayBadgeUrl | string | Form row | 16×16 image |
| homeScore | number | Form row | score |
| awayScore | number | Form row | score |
| isHome | boolean | Form row | styling |
| minutesPlayed | number | Form row | integer |
| rating | string | Form row | colored by ramp |
| goals | number | Form row | inline icon |
| assists | number | Form row | inline icon |
| yellowCards | number | Form row | inline icon |
| redCards | number | Form row | inline icon |
| subbedIn | boolean | Form row | sub icon |
| subbedOut | boolean | Form row | sub icon |
| besoccerEventCodes | string[] | Form row | event images |
| result (W/D/L) | enum | Form row | date pill bg |

### 🟡 AVAILABLE BUT UNUSED

**From FotMob Player Detail Endpoint:**
- `wcRating` (available but used as top-right badge, not in main table) 
- `clubAvgRating` / `nationalAvgRating` (weighted averages not displayed)
- `contractEnd` (fetched but not shown)

**From FotMob recentMatches (available in raw but not parsed):**
- `opponentName` (included in recentMatches struct but position codes used instead)
- `opponentTeamId` (could enable team profile click-through)
- Video highlight URLs (not included in parsed response)
- Advanced match stats per-player per-match (FotMob doesn't expose in API)

**Advanced metrics available on FotMob web but NOT via API:**
- Passing map (network graph)
- Heat map (position distribution)
- Ball recovery zones
- Progressive passes / progressive carries
- Pressure success %
- Dribble distance
- Expected assists breakdown
- Shot map / expected goals breakdown
- Tackles + interceptions vs expected
- Match-by-match detailed stats (shooting %, passing %, duels won %, etc.)

---

## 3. MATCH METRICS

### ✅ CURRENTLY USED

#### ESPN - Live Scoreboard + Team Schedule
**Source:** `https://site.api.espn.com/apis/site/v2/sports/soccer/...`

| Metric | Type | Used In | Format |
|--------|------|---------|--------|
| id | string | Match key | unique ID |
| date | unix seconds | Match time | ISO display |
| league | string | Match meta | "FIFA World Cup" |
| homeAbbr | string | Match card | 3-letter |
| awayAbbr | string | Match card | 3-letter |
| homeScore | number | Score display | integer |
| awayScore | number | Score display | integer |
| status | enum | Live badge | "live"/"scheduled"/"finished" |
| minute | number | Live timer | "45'" |

#### SofaScore - Scheduled Events + Squad Info
**Source:** `/api/v1/sport/football/scheduled-events/{date}` + team endpoints

| Metric | Type | Used In | Format |
|--------|------|---------|--------|
| tournament | string | Match header | "FIFA World Cup" |
| home | string | Match card | team name |
| away | string | Match card | team name |
| homeScore | number | Score display | integer |
| awayScore | number | Score display | integer |
| status | enum | Live badge | "live"/"scheduled"/"finished" |
| minute | number | Live timer | calculated from timestamp |

#### SofaScore - Squad Players
**Source:** `/api/v1/team/{teamId}/players`

| Metric | Type | Used In | Format |
|--------|------|---------|--------|
| id | number | Squad key | unique ID |
| name | string | Squad list | display |
| position | string | Squad list | "G"/"D"/"M"/"F" |
| shirtNumber | number | Squad list | integer |
| age | number | Squad list | integer |
| marketValue | number | Squad list | €/millions |

#### SofaScore - Recent Match Results
**Source:** `/api/v1/team/{teamId}/events`

| Metric | Type | Used In | Format |
|--------|------|---------|--------|
| id | number | Match key | unique ID |
| date | unix seconds | Result time | display |
| tournament | string | Match meta | league name |
| home | string | Match card | team name |
| away | string | Match card | team name |
| homeScore | number | Score | integer |
| awayScore | number | Score | integer |
| result (W/D/L) | enum | Form display | colored badge |

### 🟡 AVAILABLE BUT UNUSED

**From ESPN API:**
- `period` (match phase: HT, 2H, ET, PS, etc.)
- `displayClock` with period info (e.g., "45+2'")
- Season info per team
- Team IDs (numeric)
- Venue name (if available in extended response)
- Weather conditions
- Attendance
- Referee name

**From SofaScore API:**
- Stadium info (capacity, city)
- Manager/coaching staff
- Live timeline (goal times, card times, substitutions)
- Live statistics (possession %, shots, passes, etc.)
- Formation data
- Player on-field status (live substitution tracking)
- Video highlights
- Advanced live stats (passing network, heat maps, etc.)
- xG per team (real-time)
- Expected formation

---

## 4. LEADERBOARD & RANKING METRICS

### ✅ CURRENTLY USED

**From FotMob Player Leaderboards**
**Source:** `data.fotmob.com/stats/77/season/24254/{statKey}.json`

| Stat Key | Metric | Type | Used In | Format |
|----------|--------|------|---------|--------|
| goals | Top Scorers | number | Leaderboard | rank + value |
| goal_assist | Assist leaders | number | Leaderboard | rank + value |
| goal_assist (computed) | Goals + Assists | number | Leaderboard | rank + value |
| rating | Avg Rating | float | Leaderboard | 2 decimals |
| shots_on_target_per_90 | Shots on Target/90 | float | Leaderboard | 2 decimals |
| big_chance_created | Big Chances | number | Leaderboard | integer |
| chances_created | Chances Created | number | Leaderboard | integer |
| saves_per_90 (GK) | Saves/90 | float | Leaderboard | 2 decimals |
| defensive_actions_per_90 | Defensive Actions/90 | float | Leaderboard | 2 decimals |
| fouls_committed_per_90 | Fouls/90 | float | Leaderboard | 2 decimals |

**Metadata per entry:**
- rank (position)
- playerId (FotMob ID)
- playerName (display)
- countryCode (nationality)
- teamId (club)
- teamName (display)
- teamColor (brand color)
- matchesPlayed (context)
- minutesPlayed (context)

### 🟡 AVAILABLE BUT UNUSED

**From FotMob Stats API:**
- `crosses` (crossing accuracy)
- `crosses_accuracy` (%)
- `dribbles` (attempts)
- `dribbles_success` (%)
- `interceptions` (defensive)
- `aerial_duel` (won)
- `aerial_duel_accuracy` (%)
- `tackles` (defensive event)
- `tackle_accuracy` (%)
- `blocks` (defensive)
- `offsides` (discipline)
- `yellow_card` (discipline)
- `red_card` (discipline)
- `saves` (GK-specific)
- `save_percentage` (GK %)
- `clean_sheets` (GK count)
- `expected_goals` (xG)
- `expected_assists` (xA)
- And many more...

---

## 5. TRANSFERMARKT METRICS

### ✅ CURRENTLY USED

**Source:** Scraped HTML from `transfermarkt.com` (CSS selectors)

| Data | Type | Used In | Format |
|------|------|---------|--------|
| Player name | string | Team roster | display |
| Age | number | Team roster | integer |
| Position | string | Team roster | e.g. "ST" |
| Shirt number | number | Team roster | integer |
| Market value | string | Team roster | "€50.5m" |
| Nationality | string | Team roster | flag image |
| Contract end | string | Team roster | year |
| Loan status | string | Team roster | label |

### 🟡 AVAILABLE BUT NOT SCRAPED

- Performance stats (injury history)
- Transfer fee history
- Contract value (confidential, usually unavailable)
- Physical stats (weight)
- Youth career
- International caps
- Club achievements / trophies

---

## 6. BESOCCER METRICS

### ✅ CURRENTLY USED

**Source:** Scraped for player recent matches (fallback when FotMob empty)

| Data | Type | Used In | Format |
|------|------|---------|--------|
| Match date | ISO date | Form row | date pill |
| League name | string | Form row | "LaLiga" |
| Teams involved | strings | Form row | team codes |
| Score | number × 2 | Form row | "2-1" |
| Player rating | string | Form row | "7.2" |
| Events (goals, assists, cards) | enum codes | Form row | event icons |

---

## 7. POLYMARKET ODDS METRICS

### ✅ CURRENTLY USED

**Source:** `gamma-api.polymarket.com` (public CORS API, no auth)

| Metric | Type | Used In | Format |
|--------|------|---------|--------|
| tournament_id | string | Prediction market key | UUID-like |
| question | string | Prediction title | "USA to win WC?" |
| token_outcomes | string[] | Outcome labels | ["Yes", "No"] |
| token_probabilities | float[] | Implied probability | decimal 0-1 |
| last_price | float | Odds display | decimal 0.00-1.00 |
| orderbook_liquidity | float | Market depth | trading volume |

---

## SUMMARY BY SPORT DIMENSION

### Dimension: **Attacking**
✅ Goals, Assists, G+A, Shots, Shots on Target, xG, Chances Created, Big Chances  
🟡 Missed: Dribbles, Progressive passes, Pass types (through-ball, cross-field), xA per player

### Dimension: **Defending**
✅ Tackles, Defensive Contributions, Clean Sheets (team), Defensive Actions/90  
🟡 Missed: Interceptions, Blocks, Clearances, Aerial duel %, Pressure success, Dribbled past

### Dimension: **Possession/Control**
✅ Pass Accuracy %  
🟡 Missed: Possession %, Progressive carries, Ball recovery, Pressure stats, Possession zones

### Dimension: **Discipline**
✅ Yellow Cards, Red Cards  
🟡 Missed: Fouls committed (available but not surfaced for players)

### Dimension: **Goalkeeper**
✅ Saves Total, Clean Sheets, xG Conceded  
🟡 Missed: Save %, High claims, Punch distribution, Passing accuracy (GK), Sweeper activity

### Dimension: **Advanced/Variance**
✅ FotMob Rating  
🟡 Missed: xG overperformance (Goals - xG), Pressure success %, Return on investment (ROI)

---

## API ENDPOINTS REFERENCE

| Service | Endpoint | Purpose | Auth | CORS | Status |
|---------|----------|---------|------|------|--------|
| **FotMob** | `/api/data/playerData?id={id}` | Player profile + stats | None | Blocked (proxy) | ✅ |
| **FotMob** | `/api/data/teams?id={id}` | Team overview + form | None | Blocked (proxy) | ✅ |
| **FotMob** | `data.fotmob.com/stats/77/season/24254/{stat}.json` | Leaderboards | None | Blocked (proxy) | ✅ |
| **ESPN** | `site.api.espn.com/apis/site/v2/sports/soccer/...` | Live scores + schedule | None | ✅ CORS | ✅ |
| **SofaScore** | `api.sofascore.com/api/v1/sport/football/...` | Live data + squad | None | Blocked (proxy) | ✅ |
| **Transfermarkt** | `transfermarkt.com/{path}` | Team roster + values | None | Blocked (proxy) | ✅ |
| **BeSoccer** | `besoccer.com/...` (scrape) | Player recent matches | None | Blocked (scrape) | ✅ Fallback |
| **Polymarket** | `gamma-api.polymarket.com/events` | Prediction odds | None | ✅ CORS | ✅ |

---

## RECOMMENDATIONS FOR ENHANCEMENT

### Quick Wins (Low Effort)
1. **Add `Fouls Committed` to Team Stats** — already fetched, just add to grid
2. **Add `Possession %` to Team Stats** — fetch from available statKey
3. **Add Player `Dribbles/90`** — leaderboard already available
4. **Show `Loan Status` in Team Roster** — Transfermarkt already scraped

### Medium Effort
1. **Advanced Player Stats Modal** — click player to see aerials, interceptions, blocks
2. **Match Timeline** — integrate SofaScore live timeline (goals, cards, subs)
3. **Goalkeeper-specific Dashboard** — dedicated view with save %, distribution, sweeper stats
4. **Season Form Charts** — plot player rating over matches (sparkline)

### High Effort
1. **Live Heat Maps** — SofaScore data not publicly exposed; would need custom processing
2. **Passing Networks** — FotMob web only; no API access
3. **Expected Goals Deep Dive** — breakdown by shot type, area, distance
4. **Pressure/Tackle Success Analytics** — requires advanced stat modeling

---

## Data Quality Notes

- **FotMob**: Most comprehensive, 5-minute cache, WC stats after 30min played
- **ESPN**: Reliable live, no detailed stats
- **SofaScore**: Good live data, squad lists may be incomplete
- **Transfermarkt**: HTML scraped, brittle (CSS selectors can break on redesign)
- **Polymarket**: Real-time odds, liquid market = reliable implied probabilities
- **BeSoccer**: Fallback only, event codes may be incomplete

