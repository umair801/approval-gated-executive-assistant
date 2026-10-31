from app.config import Settings, load_settings


def test_defaults_are_safe_sample_mode():
    s = load_settings({})
    assert s.mode == "local" and s.effective_data_mode == "sample" and s.llm_mode == "mock" and s.validate() == []


def test_production_refuses_open_access_and_missing_key():
    errs = Settings(app_env="production").validate()
    assert any("ACCESS_TOKEN" in e for e in errs) and any("TOKEN_ENCRYPTION_KEY" in e for e in errs)


def test_production_demo_mode_is_valid_with_a_key():
    assert Settings(app_env="production", allow_public_demo=True, token_encryption_key="k").validate() == []


def test_google_mode_needs_credentials_and_key():
    errs = Settings(access_token="t", data_mode="google").validate()
    assert len(errs) == 2


def test_public_demo_can_never_use_real_data():
    s = Settings(allow_public_demo=True, data_mode="google")
    assert s.mode == "demo" and s.effective_data_mode == "sample"


def test_env_parsing():
    s = load_settings({"ALLOW_PUBLIC_DEMO": "true", "RATE_LIMIT_PER_MIN": "7", "PRICE_IN_PER_MTOK": "3.5", "ANTHROPIC_API_KEY": "k"})
    assert s.allow_public_demo and s.rate_limit_per_min == 7 and s.price_in_per_mtok == 3.5 and s.llm_mode == "claude"
