"""One-shot script: write all five wc26_stg_* parquets from in-memory builds.

Equivalent to running 14_staging_core.ipynb + 15_staging_matches.ipynb +
16_staging_players.ipynb. Used to seed the parquets before regenerating
WC26_DATA_DICTIONARY.xlsx; the notebooks themselves are still the canonical
build path that refresh.py invokes."""
import sys, warnings
sys.path.insert(0, '.')
warnings.filterwarnings('ignore')

from lib import io
import pandas as pd
import numpy as np

print('=== Building staging core (3 tables) ===')
nations  = io.load_table('wc26_nations')
stadiums = io.load_table('wc26_stadiums')
matches  = io.load_table('wc26_matches')
weather  = io.load_table('wc26_match_weather')
ref_master  = io.load_table('referee_master')
ref_profile = io.load_table('referee_profile')

io.save_table(nations.copy(), 'wc26_stg_nations')

m = matches.copy()
m['fifa_attendance_num'] = pd.to_numeric(
    m['fifa_attendance'].astype(str).str.replace(',', '', regex=False), errors='coerce')
m['is_finished'] = (m['status'] == 'finished').astype(int)
ma = (m.dropna(subset=['stadium_id']).groupby('stadium_id', dropna=False)
      .agg(match_days=('date_et', 'nunique'),
           matches_total=('match_number', 'count'),
           matches_completed=('is_finished', 'sum'),
           total_attendance_so_far=('fifa_attendance_num', 'sum'))
      .reset_index())
w = weather.dropna(subset=['stadium_id']).copy()
wa = (w.groupby('stadium_id', dropna=False)
      .agg(max_temperature_c=('temperature_c', 'max'),
           min_temperature_c=('temperature_c', 'min'),
           max_apparent_temperature_c=('apparent_temperature_c', 'max'),
           min_apparent_temperature_c=('apparent_temperature_c', 'min'),
           avg_humidity_pct=('humidity_pct', 'mean'),
           avg_dew_point_c=('dew_point_c', 'mean'),
           avg_precipitation_mm=('precipitation_mm', 'mean'),
           avg_rain_mm=('rain_mm', 'mean'),
           avg_wind_speed_kmh=('wind_speed_kmh', 'mean'),
           avg_cloud_cover_pct=('cloud_cover_pct', 'mean'))
      .reset_index())
stg_s = stadiums.merge(ma, on='stadium_id', how='left').merge(wa, on='stadium_id', how='left')
for c in ['match_days', 'matches_total', 'matches_completed', 'total_attendance_so_far']:
    stg_s[c] = stg_s[c].fillna(0).astype('Int64')
io.save_table(stg_s, 'wc26_stg_stadiums')

master_cols = ['referee_id', 'name', 'country', 'confederation', 'flag_iso',
               'nation_id', 'fm_id', 'slug', 'fm_url', 'countryApid']
io.save_table(ref_profile.merge(ref_master[master_cols], on='referee_id', how='left'),
              'wc26_stg_referee_profile')

# Fantasy player tournament-to-date totals
prs = io.load_table('fantasy_player_round_stats')
stg_fpt = (prs.groupby('fantasy_player_id', dropna=True)
              .agg(appearances=('round_id', 'count'),
                   minutes_played=('minutes_played', 'sum'),
                   starting_xi=('starting_xi', 'sum'),
                   total_points=('points', 'sum'),
                   total_goals_scored=('goals_scored', 'sum'),
                   total_assists=('assists', 'sum'),
                   clean_sheets=('clean_sheet', 'sum'),
                   saves=('saves', 'sum'),
                   tackles=('tackles', 'sum'),
                   chances_created=('chances_created', 'sum'),
                   shots_on_target=('shots_on_target', 'sum'),
                   scouting_bonus=('scouting_bonus', 'sum'),
                   yellow_cards=('yellow_cards', 'sum'))
              .reset_index())
io.save_table(stg_fpt, 'wc26_stg_fantasy_player_totals')

# Player powerrank — mean scores per (player, team) + match-count context
pr = io.load_table('wc26_player_match_powerrank')
stg_pr = (pr.groupby(['fifa_player_id', 'fifa_team_id'], dropna=False)
            .agg(avg_attacking_score=('attacking_score', 'mean'),
                 avg_defensive_score=('defensive_score', 'mean'),
                 avg_creativity_score=('creativity_score', 'mean'),
                 avg_defending_the_goal_score=('defending_the_goal_score', 'mean'),
                 n_matches_ranked=('fifa_match_id', 'count'),
                 player_kind=('player_kind', 'first'))
            .reset_index())
io.save_table(stg_pr, 'wc26_stg_player_powerrank')

# JSON emit to sibling audit-app repo
from pathlib import Path as _Path
_pr_json = _Path('E:/fifawc2026/public/data/wc26_stg_player_powerrank.json')
if _pr_json.parent.exists():
    stg_pr.to_json(_pr_json, orient='records', date_format='iso', indent=None)
    print(f'wrote {_pr_json}')
else:
    print(f'WARN: sibling data dir not found ({_pr_json.parent}) — skipping JSON emit')

print('=== Building staging matches ===')
ref_bridge = io.load_table('ref_id_bridge')
polymarket = io.load_table('wc26_polymarket_match_volume')

stadium_cols = stadiums[['stadium_id', 'capacity', 'roof_type', 'surface']].rename(
    columns={'capacity': 'stadium_capacity'})
stg = matches.merge(stadium_cols, on='stadium_id', how='left')

wc = weather[['espn_match_id', 'local_date', 'local_hour',
              'temperature_c', 'apparent_temperature_c', 'humidity_pct']].rename(
    columns={'apparent_temperature_c': 'apparent_temp_c'}).drop_duplicates(
    subset=['espn_match_id'], keep='first')
stg = stg.merge(wc, on='espn_match_id', how='left')

nation_carry = ['confederation', 'group', 'pot', 'fifa_rank', 'squad_valuation_m_eur',
                'is_host', 'espn_team_id', 'fotmob_team_id', 'tm_team_id', 'all_names']

def pref(side):
    sub = nations[['nation_id'] + nation_carry].copy()
    sub = sub.rename(columns={c: f'{side}_{c}' for c in nation_carry})
    sub = sub.rename(columns={'nation_id': f'{side}_nation_id'})
    return sub

stg = stg.merge(pref('home'), on='home_nation_id', how='left')
stg = stg.merge(pref('away'), on='away_nation_id', how='left')

ref_carry = ['referee_id', 'confederation', 'flag_iso', 'nation_id', 'fm_id', 'slug', 'fm_url']
ref_pref = ref_master[ref_carry].rename(columns={c: f'referee_{c}' for c in ref_carry})
bc = ref_bridge[['fifa_referee_id', 'referee_id']].dropna(subset=['fifa_referee_id']).drop_duplicates(
    subset=['fifa_referee_id']).copy()
stg['_fr'] = stg['fifa_referee_id'].astype('string')
bc['_fr'] = bc['fifa_referee_id'].astype('string')
stg = stg.merge(bc[['_fr', 'referee_id']].rename(columns={'referee_id': 'referee_referee_id'}),
                on='_fr', how='left').drop(columns=['_fr'])
stg = stg.merge(ref_pref, on='referee_referee_id', how='left')

poly = polymarket[['espn_match_id', 'volume_moneyline', 'volume_other']].drop_duplicates(
    subset=['espn_match_id'])
stg = stg.merge(poly, on='espn_match_id', how='left')
io.save_table(stg, 'wc26_stg_matches')

print('=== Building staging players ===')
enrichment   = io.load_table('wc26_player_enrichment')
career_club  = io.load_table('wc26_player_career_club_summary')
career_natl  = io.load_table('wc26_player_career_national_summary')
market_value = io.load_table('wc26_player_market_value_summary')
fotmob_wc    = io.load_table('wc26_player_fotmob_wc')
match_wide   = io.load_table('wc26_player_match_stats_wide')
recent_form  = io.load_table('wc26_player_recent_matches_fotmob')

club_un = {'fifa_player_id', 'current_club_fotmob_id', 'current_club_name', 'all_clubs', 'num_total_clubs'}
club = career_club.rename(columns={c: f'club_{c}' for c in career_club.columns if c not in club_un})
natl = career_natl.rename(columns={c: f'national_{c}' for c in career_natl.columns if c != 'fifa_player_id'})
val  = market_value.rename(columns={c: f'value_{c}' for c in market_value.columns if c != 'fifa_player_id'})
fwc_trim = fotmob_wc.drop(columns=['fotmob_player_id', 'season_name', 'fotmob_tournament_id'], errors='ignore')
fwc = fwc_trim.rename(columns={c: f'fotmob_wc_{c}' for c in fwc_trim.columns if c != 'fifa_player_id'})

stg = (enrichment
       .merge(club, on='fifa_player_id', how='left')
       .merge(natl, on='fifa_player_id', how='left')
       .merge(val,  on='fifa_player_id', how='left')
       .merge(fwc,  on='fifa_player_id', how='left'))

SUM_COLS = ['Assists', 'AttemptAtGoal', 'AttemptAtGoalOnTarget',
            'AttemptedBallProgressions', 'AttemptedSwitchesOfPlay',
            'CleanSheets', 'CompletedBallProgressions', 'CompletedSwitchesOfPlay', 'Corners',
            'Crosses', 'CrossesCompleted', 'DefensivePressuresApplied', 'DistanceHighSpeedSprinting',
            'DistanceWalking', 'DistributionsCompletedUnderPressure', 'DistributionsUnderPressure',
            'ForcedTurnovers', 'FoulsAgainst', 'FoulsFor', 'FreeKicks', 'GoalkeeperSaves',
            'Goals', 'GoalsConceded', 'GoalsOutsideThePenaltyArea', 'LinebreaksAttempted',
            'LinebreaksAttemptedCompleted', 'LinebreaksCompletedUnderPressure',
            'NumberOfInvolvements', 'NumberOfPossessionSequences', 'NumberOfShotEndingSequences',
            'OffersToReceiveTotal', 'Offsides', 'OwnGoals', 'Passes', 'PassesCompleted',
            'Penalties', 'PenaltiesScored', 'ReceivedOffersToReceive',
            'ReceptionsBetweenMidfieldAndDefensiveLine', 'ReceptionsInBehind',
            'ReceptionsUnderNoPressure', 'ReceptionsUnderPressure', 'RedCards', 'SpeedRuns',
            'Sprints', 'SubstitutionsIn', 'SubstitutionsOut', 'TakeOnsCompleted', 'TimePlayed',
            'TotalDistance', 'YellowCards']
AVG_COLS = ['AvgSpeed', 'XG']
MAX_COLS = ['TopSpeed']
present = set(match_wide.columns)
agg_spec = {**{c: 'sum' for c in SUM_COLS if c in present},
            **{c: 'mean' for c in AVG_COLS if c in present},
            **{c: 'max' for c in MAX_COLS if c in present}}
stats_agg = match_wide.groupby('fifa_player_id', dropna=True).agg(agg_spec).reset_index()
stats_agg = stats_agg.rename(columns={c: f'fifa_wc_{c}' for c in agg_spec.keys()})

mw = match_wide.copy()
mw['_opp'] = np.where(mw['nation_id'] == mw['home_nation_id'], mw['away_nation_id'], mw['home_nation_id'])

def _su(s):
    return sorted({v for v in s.dropna().tolist() if v is not None and not (isinstance(v, float) and np.isnan(v))})

arr = mw.groupby('fifa_player_id', dropna=True).agg(
    stages_played=('stage', _su),
    opponents=('_opp', _su),
    match_ids=('fifa_match_id', _su),
).reset_index()
stg = stg.merge(stats_agg, on='fifa_player_id', how='left').merge(arr, on='fifa_player_id', how='left')
for c in ('stages_played', 'opponents', 'match_ids'):
    stg[c] = stg[c].apply(lambda v: v if isinstance(v, list) else [])

recent = recent_form.copy()
recent['_md'] = pd.to_datetime(recent['match_date_utc'], utc=True, errors='coerce')
recent = recent.sort_values(['fifa_player_id', '_md'], ascending=[True, False])

WINDOWS = (5, 10, 15, 20)

def build_window(df, n):
    top = df.groupby('fifa_player_id', group_keys=False, sort=False).head(n)
    g = top.groupby('fifa_player_id', sort=False)
    block = g.agg(
        matches_played=('match_id_fotmob', 'count'),
        minutes_played=('minutes_played', 'sum'),
        goals=('goals', 'sum'),
        assists=('assists', 'sum'),
        yellow_cards=('yellow_cards', 'sum'),
        red_cards=('red_cards', 'sum'),
        player_of_the_match=('player_of_the_match', 'sum'),
        on_bench_sum=('on_bench', 'sum'),
    )
    rm = g.apply(lambda gr: gr.loc[gr['fotmob_rating'] > 0, 'fotmob_rating'].mean())
    block['fotmob_rating'] = rm
    block['started_pct'] = np.where(
        block['matches_played'] > 0,
        1 - (block['on_bench_sum'] / block['matches_played']),
        np.nan,
    )
    block['has_data'] = (block['matches_played'] > 0)
    block = block.drop(columns=['on_bench_sum'])
    block = block.rename(columns={c: f'recent{n}_{c}' for c in block.columns})
    return block.reset_index()

for n in WINDOWS:
    stg = stg.merge(build_window(recent, n), on='fifa_player_id', how='left')
for n in WINDOWS:
    stg[f'recent{n}_has_data'] = stg[f'recent{n}_has_data'].fillna(False).astype(bool)
    for c in ('matches_played', 'minutes_played', 'goals', 'assists',
              'yellow_cards', 'red_cards', 'player_of_the_match'):
        stg[f'recent{n}_{c}'] = stg[f'recent{n}_{c}'].fillna(0)

io.save_table(stg, 'wc26_stg_players')
print(f'final wc26_stg_players: {len(stg)} rows, {len(stg.columns)} cols')

# === wc26_stg_players_view — slim curated view + derived ratios ===
SLIM_COLS = [
    'nation_id', 'fifa_player_id', 'name', 'short_name', 'birth_date',
    'jersey_num', 'height_cm', 'weight_kg', 'position', 'preferred_foot',
    'picture_url', 'fotmob_player_id', 'fotmob_name', 'club_fotmob_id',
    'club_name', 'position_ids_desc', 'wc_rating',
    'tm_player_id', 'tm_slug', 'club_tm_id', 'club_name_tm', 'contract_end',
    'club_senior_appearances', 'club_senior_goals', 'club_senior_assists',
    'club_senior_weighted_avg_rating', 'club_senior_num_seasons',
    'current_club_name', 'current_club_fotmob_id', 'all_clubs', 'num_total_clubs',
    'national_senior_appearances', 'national_senior_goals', 'national_senior_assists',
    'national_senior_weighted_avg_rating', 'national_senior_num_seasons',
    'value_fotmob_latest_eur', 'value_fotmob_latest_date',
    'value_fotmob_peak_eur', 'value_fotmob_peak_date',
    'value_tm_latest_eur', 'value_tm_latest_date',
    'value_tm_peak_eur', 'value_tm_peak_date',
    'fotmob_wc_appearances', 'fotmob_wc_fotmob_rating',
    'fotmob_wc_chances_created', 'fotmob_wc_big_chances_created',
    'fotmob_wc_dribbles', 'fotmob_wc_successful_dribbles_pct',
    'fotmob_wc_duels_won', 'fotmob_wc_duels_won_pct',
    'fotmob_wc_touches', 'fotmob_wc_touches_opp_box',
    'fotmob_wc_defensive_contributions', 'fotmob_wc_tackles',
    'fotmob_wc_xg_against_on_pitch',
    'fifa_wc_Goals', 'fifa_wc_Assists', 'fifa_wc_CleanSheets',
    'fifa_wc_GoalkeeperSaves', 'fifa_wc_GoalsConceded',
    'fifa_wc_GoalsOutsideThePenaltyArea', 'fifa_wc_TimePlayed',
    'fifa_wc_ReceptionsBetweenMidfieldAndDefensiveLine', 'fifa_wc_ReceptionsInBehind',
    'fifa_wc_ReceptionsUnderNoPressure', 'fifa_wc_ReceptionsUnderPressure',
    'fifa_wc_ReceivedOffersToReceive', 'fifa_wc_OffersToReceiveTotal',
    'fifa_wc_PassesCompleted', 'fifa_wc_Passes',
    'fifa_wc_CompletedBallProgressions', 'fifa_wc_AttemptedBallProgressions',
    'fifa_wc_CompletedSwitchesOfPlay', 'fifa_wc_AttemptedSwitchesOfPlay',
    'fifa_wc_CrossesCompleted', 'fifa_wc_Crosses',
    'fifa_wc_DistributionsCompletedUnderPressure', 'fifa_wc_DistributionsUnderPressure',
    'fifa_wc_LinebreaksCompletedUnderPressure', 'fifa_wc_LinebreaksAttemptedCompleted',
    'fifa_wc_LinebreaksAttempted',
    'fifa_wc_DistanceWalking', 'fifa_wc_DistanceHighSpeedSprinting', 'fifa_wc_TotalDistance',
    'fifa_wc_AttemptAtGoal', 'fifa_wc_AttemptAtGoalOnTarget',
    'fifa_wc_RedCards', 'fifa_wc_YellowCards',
    'fifa_wc_AvgSpeed', 'fifa_wc_TopSpeed', 'fifa_wc_XG',
    'fifa_wc_Corners', 'fifa_wc_FoulsAgainst', 'fifa_wc_DefensivePressuresApplied',
    'fifa_wc_SpeedRuns', 'fifa_wc_Sprints', 'fifa_wc_FoulsFor', 'fifa_wc_OwnGoals',
    'fifa_wc_NumberOfInvolvements', 'fifa_wc_ForcedTurnovers', 'fifa_wc_Offsides',
    'fifa_wc_FreeKicks',
    'fifa_wc_NumberOfPossessionSequences', 'fifa_wc_NumberOfShotEndingSequences',
    'fifa_wc_Penalties', 'fifa_wc_PenaltiesScored', 'fifa_wc_TakeOnsCompleted',
    'stages_played', 'opponents', 'match_ids',
    'recent5_minutes_played', 'recent5_goals', 'recent5_assists',
    'recent5_yellow_cards', 'recent5_red_cards', 'recent5_player_of_the_match',
    'recent5_fotmob_rating', 'recent5_has_data', 'recent5_started_pct',
    'recent10_minutes_played', 'recent10_goals', 'recent10_assists',
    'recent10_yellow_cards', 'recent10_red_cards', 'recent10_player_of_the_match',
    'recent10_fotmob_rating', 'recent10_has_data', 'recent10_started_pct',
    'recent15_minutes_played', 'recent15_goals', 'recent15_assists',
    'recent15_yellow_cards', 'recent15_red_cards', 'recent15_player_of_the_match',
    'recent15_fotmob_rating', 'recent15_started_pct', 'recent15_has_data',
]
present = [c for c in SLIM_COLS if c in stg.columns]
slim = stg[present].copy()

def _div(num_col, den_col):
    if num_col not in slim.columns or den_col not in slim.columns:
        return pd.Series([np.nan]*len(slim), index=slim.index)
    n = slim[num_col]; d = slim[den_col]
    return np.where(pd.notna(d) & (d != 0) & pd.notna(n), n / d, np.nan)

slim['fifa_wc_mid_def_reception_pct']                       = _div('fifa_wc_ReceptionsBetweenMidfieldAndDefensiveLine', 'fifa_wc_ReceivedOffersToReceive')
slim['fifa_wc_attacking_reception_pct']                     = _div('fifa_wc_ReceptionsInBehind', 'fifa_wc_ReceivedOffersToReceive')
slim['fifa_wc_under_vs_no_pressure_reception_ratio']        = _div('fifa_wc_ReceptionsUnderPressure', 'fifa_wc_ReceptionsUnderNoPressure')
slim['fifa_wc_reception_completion_pct']                    = _div('fifa_wc_ReceivedOffersToReceive', 'fifa_wc_OffersToReceiveTotal')
slim['fifa_wc_pass_completion_pct']                         = _div('fifa_wc_PassesCompleted', 'fifa_wc_Passes')
slim['fifa_wc_ball_progression_completion_pct']             = _div('fifa_wc_CompletedBallProgressions', 'fifa_wc_AttemptedBallProgressions')
slim['fifa_wc_switches_of_play_completion_pct']             = _div('fifa_wc_CompletedSwitchesOfPlay', 'fifa_wc_AttemptedSwitchesOfPlay')
slim['fifa_wc_cross_completion_pct']                        = _div('fifa_wc_CrossesCompleted', 'fifa_wc_Crosses')
slim['fifa_wc_distributions_under_pressure_completion_pct'] = _div('fifa_wc_DistributionsCompletedUnderPressure', 'fifa_wc_DistributionsUnderPressure')
slim['fifa_wc_linebreaks_under_pressure_proportion_pct']    = _div('fifa_wc_LinebreaksCompletedUnderPressure', 'fifa_wc_LinebreaksAttemptedCompleted')
slim['fifa_wc_linebreaks_completion_pct']                   = _div('fifa_wc_LinebreaksAttemptedCompleted', 'fifa_wc_LinebreaksAttempted')
slim['fifa_wc_pct_distance_walking']                        = _div('fifa_wc_DistanceWalking', 'fifa_wc_TotalDistance')
slim['fifa_wc_pct_distance_high_speed_sprinting']           = _div('fifa_wc_DistanceHighSpeedSprinting', 'fifa_wc_TotalDistance')
yc = slim['fifa_wc_YellowCards'].fillna(0) if 'fifa_wc_YellowCards' in slim.columns else 0
rc = slim['fifa_wc_RedCards'].fillna(0)    if 'fifa_wc_RedCards'    in slim.columns else 0
slim['fifa_wc_TotalCards'] = yc + rc
io.save_table(slim, 'wc26_stg_players_view')

# JSON emit to sibling audit-app repo
from pathlib import Path as _Path
SIBLING_JSON = _Path('E:/fifawc2026/public/data/wc26_stg_players_view.json')
if SIBLING_JSON.parent.exists():
    slim.to_json(SIBLING_JSON, orient='records', date_format='iso', indent=None)
    print(f'wrote {SIBLING_JSON}')
else:
    print(f'WARN: sibling data dir not found ({SIBLING_JSON.parent}) — skipping JSON emit')

print('done.')
