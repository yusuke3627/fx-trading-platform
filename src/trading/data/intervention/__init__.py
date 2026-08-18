"""FX intervention PIT dataset (research note 2026-08-15, Phase C).

Recognition stages are separate events with their own known_at — what the
market suspected, what the press reported, what MOF later confirmed — and a
later stage never overwrites an earlier one. Official amounts come from MOF
publications (mof.py); the market-recognition timeline is curated
(config/intervention_episodes.yaml) because official statistics cannot say
when the market learned of an intervention.
"""
