from soccer_bot.utils import normalize_team_name


def test_normalize_team_name_strips_accents_and_tokens() -> None:
    assert normalize_team_name("FC København") == "kobenhavn"
    assert normalize_team_name("Atlético de Madrid") == "atletico madrid"
    assert normalize_team_name("AFC Bournemouth") == "bournemouth"
