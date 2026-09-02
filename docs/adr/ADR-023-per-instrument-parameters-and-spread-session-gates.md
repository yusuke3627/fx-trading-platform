# ADR-023: 通貨ペア別パラメータと spread / session gate

**Status:** Accepted (2026-09-02)

## Context

Strategy の flat な `parameters` と Risk の単一 `max_spread_pips` は USDJPY
だけを前提としていた。通貨ペアを増やすと、ATR 水準や pip size、取引時間帯が
異なるため、同じ固定値を全ペアへ適用できない。

## Decision

Strategy パラメータを defaults と通貨ペア別 override に分け、Strategy は
設定 dict を直接探索せず `StrategyParameterResolver` の解決結果だけを読む。
既存の flat な Strategy パラメータは defaults として読み込む。

spread gate は二層にする。Strategy 層は spread / ATR の無次元比を primary
gate とし、必要なら通貨ペア別の absolute ceiling も併用する。Risk 層は ATR を
持たず、通貨ペア別 `absolute_max_spread_pips` を hard safety ceiling とする。
ceiling 未設定の通貨ペアは `SYMBOL_LIMIT_CONFIGURED` と同じ考え方で fail-close
する。USDJPY 以外の初期 ceiling は保守的な仮置きであり、各ペアの昇格 Gate で
校正する。`spread_gate` 群と `absolute_max_spread_pips` の型は設定境界
（`StrategyParameters` の読み込み時）で確定し、不正な設定は最初の市場イベント
ではなく起動時に拒否する。

session profile は tokyo / london / new_york の名前参照として定義し、Strategy
パラメータから profile 名を解決する。runtime の entry gate への配線は、並行して
進める session 判定の IANA timezone 化が完了した後の follow-up とする。

## Consequences

`RiskConfig.max_spread_pips` は削除し、旧 YAML キーの互換シムは設けない。
Risk の ceiling を設定していない通貨ペアでは新規 entry が拒否される。
Scalping の profile は全 session を `SHADOW_ONLY` とし、引き続き research 評価
だけを許す。session profile の runtime 配線までは Strategy の entry 挙動を
変えない。
