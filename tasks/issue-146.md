# issue #146: micro_live overlay で post_event_failed_breakout を SHADOW へ降格する（H5 判定の反映）

## 要件（issue #146 本文の転記。Codex は issue を読みに行かない前提で全文をここに置く）

### 背景

H5（`post_event_failed_breakout` のマクロ確認レッグの ablation、研究ノート
`docs/research/2026-09-10-h5-macro-confirmation-ablation.md`）の判定は「差が検出できない」で、
両腕とも 2 年間で損失だった。戦略を live へ昇格させる根拠は無い。

一方 `config/micro_live.yaml` は現在、`trading_enabled: true` のうえでこの戦略を
`enabled: true` / `status: MICRO_LIVE`（live_eligible）にしている。`--env micro_live` で起動すると、
判定に反して最大 1,000 通貨の実注文が出る。

### issue が求めること

- `config/micro_live.yaml` の `post_event_failed_breakout` を無効化するか SHADOW 段階へ降格する
- 判定を覆す材料が出るまで戻さない
- 設定のテスト（`tests/unit` の config ローダー）に、micro_live で live_eligible な戦略が
  昇格判定済みのものだけであることを固定できるなら足す

### ユーザー決定（議論は不要。この通りに実装する）

1. **対応方針は SHADOW への降格**（無効化ではない）。`config/micro_live.yaml` の
   `post_event_failed_breakout` を `status: SHADOW` にし、`enabled: true` は維持する
   （micro_live 環境でも signal の記録は続け、注文は出さない）
2. 判定を覆す材料が出るまで戻さない
3. テストは `tests/unit/test_config.py` に「micro_live 環境で live_eligible
   （MICRO_LIVE / LIMITED_LIVE / PRODUCTION）な戦略が存在しない（= 昇格判定済みの戦略が無い）」
   ことを固定するテストを足す。将来昇格判定済みの戦略が出たときに更新する前提で、
   現時点の期待値はゼロ本
4. YAML のコメントは WHY だけ 1〜2 行（下記の文言そのまま）。PR 番号や AI レビューの引用は書かない
5. ファイル冒頭の「Micro live: 1,000-unit cap …」の説明は、live_eligible な戦略が現在 0 本である
   ことと矛盾しないよう必要最小限だけ直す（下記の文言そのまま）

---

## 現状の把握（調査済み。Codex が再調査する必要はない）

基準リビジョンは worktree HEAD = `origin/main` = `4945335`（2026-09-11 fetch 済み）。

- `config/micro_live.yaml`（全 15 行）:

  ```yaml
  # Micro live: 1,000-unit cap, broker-side SL mandatory. Requires every item
  # of the Production Gate checklist (docs/SYSTEM_SPEC.md) verified first.
  risk:
    trading_enabled: true
    max_open_positions_per_symbol: 1
    # 多ペア live を明示的に判断するまで、portfolio 全体でも従来どおり 1 本。
    max_open_positions_portfolio: 1
    max_units_per_symbol:
      USDJPY: 1000
    require_broker_stop_loss: true

  strategies:
    post_event_failed_breakout:
      enabled: true
      status: MICRO_LIVE
  ```

- overlay の合成は `src/trading/config.py:165` `_deep_merge` と `:175` `load_config`。
  `config/base.yaml:158-173` の `post_event_failed_breakout` は `enabled: false` /
  `status: RESEARCH_ONLY` で、overlay の `enabled` / `status` がそれを上書きする
- live_eligible の定義:
  - `src/trading/strategy/base.py:48-50`
    `LIVE_ELIGIBLE_STATUSES = frozenset({MICRO_LIVE, LIMITED_LIVE, PRODUCTION})`
  - `src/trading/runner.py:32-34` `StrategyBinding.live_eligible` は
    `self.status in LIVE_ELIGIBLE_STATUSES`。`dispatch` が `CollectedSignal` に転記する判定値
  - `StrategyConfig`（`src/trading/strategy/base.py:89`）自体には live_eligible 判定は無い。
    テストは `LIVE_ELIGIBLE_STATUSES` を import して `config.strategies[*].status` と突き合わせる
- `StrategyConfig.runs`（`src/trading/strategy/base.py:133`）は `enabled` が真かつ `DISABLED`
  以外なら真。`enabled: true` / `SHADOW` は評価を継続し、`StrategyRunner.dispatch` は
  `live_eligible=False` のシグナルを返す（signal の記録は続く、というユーザー決定 1 の根拠）
- 現在 `--env` を受け取る live 側の entrypoint は `src/trading/live/shadow.py:585-597`
  （`ShadowRunner`）だけで、OMS も broker adapter も持たず status にかかわらず発注しない。
  つまり現行コードでは micro_live overlay から実注文へ至る経路は無く、issue 本文の
  「最大 1,000 通貨の実注文が出る」は将来 live runner を配線したときの話。本変更は
  「昇格根拠の無い戦略に live 対象の status を与えている」設定そのものを正すもので、
  config テストの保証範囲も「合成後の設定に live 対象の status が無い」ことまで
  （発注 API の非呼び出しは保証対象に含めない）
- 既存テストで micro_live の status を固定しているのは
  `tests/unit/test_config.py:67-77` `test_micro_live_overlay_caps_and_enables` の
  `assert strategy.status is StrategyStatus.MICRO_LIVE`（74 行目）だけ。**本変更で SHADOW に
  変わるので、このアサーションは `StrategyStatus.SHADOW` に更新する**（テストを緩めるのではなく、
  設定変更に追随させる）。`enabled is True` と base パラメータの継承アサーションはそのまま
- `tests/unit/test_live_wiring.py:183-187` は micro_live の戦略名が実装済みの戦略集合に
  含まれることだけを見ており、status には触れない → 変更不要
- `tests/unit/test_runner.py` の SHADOW シグナル収集（`live_eligible=False`）と
  `tests/unit/test_shadow_runner.py` は変更せず、unit suite でそのまま実行する
- `config/shadow.yaml` の同戦略は既に `enabled: true` / `status: SHADOW`。micro_live も
  同じ形になる（shadow.yaml は変更しない）
- 研究ノート `docs/research/2026-09-10-h5-macro-confirmation-ablation.md` は **PR #145 で
  追加予定でまだ main には無い**。YAML コメントにはこのパスをそのまま書く。ファイルを
  作らない・別のパスに直さない

---

## 設計（この通りに実装すること）

### 1. `config/micro_live.yaml`

変更後の全文（コメント文言もこの通り）:

```yaml
# Micro live: 1,000-unit cap, broker-side SL mandatory. Requires every item
# of the Production Gate checklist (docs/SYSTEM_SPEC.md) verified first.
# 現時点で live_eligible（MICRO_LIVE / LIMITED_LIVE / PRODUCTION）な戦略は無く、
# この overlay で起動しても実注文は出ない。
risk:
  trading_enabled: true
  max_open_positions_per_symbol: 1
  # 多ペア live を明示的に判断するまで、portfolio 全体でも従来どおり 1 本。
  max_open_positions_portfolio: 1
  max_units_per_symbol:
    USDJPY: 1000
  require_broker_stop_loss: true

strategies:
  # H5 判定（docs/research/2026-09-10-h5-macro-confirmation-ablation.md）で昇格根拠なし。
  # 再測定で裏付けが出るまで SHADOW。
  post_event_failed_breakout:
    enabled: true
    status: SHADOW
```

差分は次の 3 点だけ:

- 冒頭コメントに 3〜4 行目（「現時点で live_eligible … 実注文は出ない。」）を追加
- `post_event_failed_breakout` の直前に WHY コメント 2 行を追加
- `status: MICRO_LIVE` → `status: SHADOW`

`risk:` ブロック（`trading_enabled: true` を含む）と `enabled: true` は変更しない。
戦略の追加・削除もしない。

### 2. `tests/unit/test_config.py`

#### 2-a. 既存テストの追随（74 行目）

```python
def test_micro_live_overlay_caps_and_enables():
    ...
    strategy = config.strategies["post_event_failed_breakout"]
    assert strategy.status is StrategyStatus.SHADOW   # ← MICRO_LIVE から変更
    assert strategy.enabled is True
    ...
```

テスト名・他のアサーションは変えない。

#### 2-b. 新規テストの追加（`test_micro_live_overlay_caps_and_enables` の直後に置く）

```python
def test_micro_live_has_no_live_eligible_strategy_until_one_is_promoted():
    # live_eligible な status は実注文を出す側（runner.StrategyBinding.live_eligible）。
    # 昇格判定を通った戦略は現時点で無い（post_event_failed_breakout は H5 判定で
    # 根拠なし）ので、合成後の設定に live 対象の status が無いことをここで固定する。
    # 昇格判定済みの戦略が出たら、その strategy_id をこの期待値に足す。
    config = load_config("micro_live", CONFIG_DIR)

    live_eligible = sorted(
        strategy_id
        for strategy_id, strategy in config.strategies.items()
        if strategy.status in LIVE_ELIGIBLE_STATUSES
    )
    assert live_eligible == []
```

- import は既存の `from trading.strategy.base import StrategyStatus` を
  `from trading.strategy.base import LIVE_ELIGIBLE_STATUSES, StrategyStatus` に変える
  （ruff の isort 順に従う。`LIVE_ELIGIBLE_STATUSES` が先）
- 期待値は空リスト。`assert not live_eligible` ではなく `== []` にして、落ちたときに
  どの strategy_id が live_eligible かがそのまま assertion message に出るようにする
- 判定に `LIVE_ELIGIBLE_STATUSES` を使い、status の列挙をテスト側に複製しない
  （`runner.py` と同じ定義に固定する）
- このテストの保証は「合成後の設定に live 対象の status が無い」こと。発注 API の非呼び出しや
  シグナルの DB 保存は保証に含めない
- `enabled` は見ない（issue の争点は status。`enabled: true` のまま SHADOW で記録を続けるのが
  ユーザー決定）

### 3. 触らないもの

- `src/` 配下は一切変更しない（`StrategyConfig` に live_eligible プロパティを足す等の
  "便利化" はしない。テストは `LIVE_ELIGIBLE_STATUSES` を直接 import する）
- `config/shadow.yaml` / `config/production.yaml` / `config/base.yaml` /
  `config/demo.yaml` / `config/backtest.yaml`
- `docs/`（研究ノートを含む。PR #145 は未マージ）、`docs/adr/`（ADR 不要: 設計変更ではなく
  設定の段階変更）
- `migrations/`（DB マイグレーションは不要）
- `tests/unit/test_live_wiring.py` / `tests/unit/test_runner.py` /
  `tests/unit/test_session_entry_gate.py` / `tests/unit/test_shadow_runner.py`
  （status を固定しているのは `test_config.py` だけ）

---

## 変更対象ファイル

| ファイル | 変更内容 |
| --- | --- |
| `config/micro_live.yaml` | `status: MICRO_LIVE` → `SHADOW`、WHY コメント 2 行、冒頭コメントに 2 行追記（上記の全文どおり） |
| `tests/unit/test_config.py` | 74 行目のアサーションを `SHADOW` に更新、`LIVE_ELIGIBLE_STATUSES` を import、新規テスト 1 本を追加 |
| `tasks/issue-146.md` | 本ファイル（コミット対象） |

**DB マイグレーションは不要**（`migrations/` に触らない）。UI は無い。

運用上の補足（実装作業ではない）: `trading.live.shadow.main` は起動時に一度だけ `load_config` を
呼ぶので、稼働ホストへは config 更新後のプロセス再起動で反映される。本タスクではホスト側の
再起動・注文・建玉の操作は行わない。

## テスト方針

- `tests/unit/test_config.py` の既存テストは、設定変更に追随する 74 行目以外は変えない
- 新規テストは上記 2-b の 1 本のみ。パラメタライズや production 環境への拡張はしない
  （ユーザー決定は micro_live のみ）
- `tests/unit/test_invariants.py` を通すためにテスト側を緩めない（本変更では触れない）

## 完了条件（実行可能なコマンドで）

worktree のルートで、すべて green になること:

```bash
.venv/bin/ruff check .
.venv/bin/pytest tests/unit tests/replay tests/failure
```

- `tests/unit` / `tests/replay` / `tests/failure` は AGENTS.md でローカル常時実行の対象
- `tests/broker` は MT5 なし環境で自動 skip される（skip は正常）。`tests/integration` は
  PostgreSQL 必須なので上記コマンドの対象外（push 時に Claude 側が使い捨て DB を用意して
  pre-push hook の `pytest -q` で流す）
- 実装後の `git status --short` が、追跡ファイルの変更として `config/micro_live.yaml` と
  `tests/unit/test_config.py` の 2 件だけを示すこと（`tasks/issue-146.md` と `tmp/` は
  Claude が事前に置いた未追跡ファイルで、そのまま残す）

## やらないこと（スコープ外。手を出さない）

- `config/shadow.yaml` / `config/production.yaml` / `config/base.yaml` の変更
- 研究ノート（`docs/research/`、PR #145 は未マージ）の変更・作成
- 戦略コード・Risk・OMS・storage の変更（`src/` は一切触らない）
- `docs/adr/` への ADR 追加
- 周辺リファクタ・無関係な整形・追加の抽象化（`StrategyConfig.live_eligible` の追加、
  テストのパラメタライズ化、他環境への同種テストの横展開）
- `tmp/` 配下（Codex のログ・セッション ID）と `tasks/APPROVAL.md` / `tasks/PARENT-NOTES.md`
  への変更
- `git commit`（コミットと PR は Claude 側が行う）

## プロジェクト規約（`AGENTS.md` / `.claude/rules/*.md` 由来。Codex 向けに転記）

- コミットしない（本タスクの分担としてコミットと PR は Claude 側が行う）
- YAML・テストのコメントは日本語を優先する（既存の英語コメントは維持してよい）
- WHAT を説明するコメントを書かない。「○○のために追加」などコミット文脈依存のコメント、
  PR 番号、AI レビューの引用をコード・設定のコメントに残さない
- テストは実装の写経にしない。通すために既存テスト（特に `tests/unit/test_invariants.py`）
  を緩めない
- テストデータに実在の人物・団体名を使わない
- 検証はシステム境界のみ。内部関数間に防御的分岐を足さない
- lint は ruff（`pyproject.toml` の設定）。`ruff check .` が無指摘で通ること
