# issue #92: 政府が公式表明した介入を GOVERNMENT_CONFIRMED としてタイムラインに載せる

## 背景（issue の要点）

介入リスクの検証段階（`src/trading/data/intervention/features.py:27` の `KIND_TO_STATUS`）は
イベント種別を `src/trading/intelligence/intervention.py:18` の `VERIFICATION_ORDER`
（RUMOR → MARKET_SUSPECTED → MEDIA_CONFIRMED → OFFICIAL_ACTION_CONFIRMED →
OFFICIAL_AMOUNT_CONFIRMED）へ写し、`verification_state_level` が `(index + 1) / 5` で
[0, 1] へ写像する（MEDIA_CONFIRMED = 0.6、OFFICIAL_ACTION_CONFIRMED = 0.8、
OFFICIAL_AMOUNT_CONFIRMED = 1.0）。

events には `INTERVENTION_REPORTED`（報道段階 = 0.6）と公式の月次額・日次額
（= 1.0）しか無く、**政府の公式表明 `INTERVENTION_GOVERNMENT_CONFIRMED`（= 0.8）が
1 件も生成されていない**。金額の一次資料は数か月遅れて届くため、介入直後に取れる
最良の確信度が 0.6 で頭打ちになる。`INTERVENTION_MARKET_SUSPECTED` は一次資料が
無いので対象外。

## 解決方針（ユーザー判断 2026-09-06）

新しいコレクターは作らない。`config/intervention_episodes.yaml`（市場認識タイムライン）に
`kind: GOVERNMENT_CONFIRMED` のエントリを一次資料つきで足す。

ローダー `src/trading/data/intervention/episodes.py` は変更不要（調査済み）:

- `RecognitionKind`（`episodes.py:26`）は `GOVERNMENT_CONFIRMED` を既に受け付ける
- `load_episodes`（`episodes.py:54`）の重複キーは `(kind, action_date)` なので、同じ
  `action_date` の REPORTED エントリと共存できる
- `event_from_recognition`（`episodes.py:69`）は event_type を
  `INTERVENTION_{kind}`、event_id を `uuid5(NAMESPACE_URL,
  "intervention-recognition:{kind}:{action_date}")` で決定的に生成する
- 既存の intervention collector（`python -m trading.data.intervention.collector --env demo`、
  `collector.py:62`）を再実行すれば `insert_new` で events に入る

**対象は「政府が実施を公式に表明した回」だけ**。覆面介入（2022-10-21/24、
2024-04-29/05-01/07-11/07-12、2026-04-30/05-04/05-06、2026-07-30 の単独介入）は当局が
「コメントしない」を貫いており、確定段階は月次額・日次額の公表（既存の
`INTERVENTION_OFFICIAL_*_AMOUNT` イベント）なので追加しない。

## 変更 1: `config/intervention_episodes.yaml`

### 1-a. 先頭コメントへの追記

先頭コメントブロック（`# - kind: MARKET_SUSPECTED / REPORTED / GOVERNMENT_CONFIRMED`
の行の直後）に、次の趣旨を 2〜3 行で足す:

```
# - GOVERNMENT_CONFIRMED は政府が実施を公式表明した回だけ置く。覆面介入は
#   月次額・日次額の公表（MOF collector のイベント）が確定段階なので置かない。
#   MARKET_SUSPECTED は一次資料が無いので置かない
```

### 1-b. エントリ追加（この内容をそのまま使う。一次資料は確認済み）

2022 セクション（`action_date: 2022-09-22` の REPORTED エントリの直後）に:

```yaml
  - kind: GOVERNMENT_CONFIRMED
    action_date: 2022-09-22
    known_at: 2022-09-22T09:34:00+00:00
    direction: JPY_BUY
    verified: true
    source_uri: https://www.fsa.go.jp/common/conference/minister/2022b/20220922-1.html
    note: 財務大臣・財務官の共同記者会見（18:34〜19:03 JST）冒頭で「本日為替介入を実施いたしました」と明言。財務官は 17 時台に記者団へ「断固たる措置に踏み切った」と先に発言しているが、一次資料で時刻を確認できる会見開始を known_at に置く
```

2026 セクション（`action_date: 2026-07-31` の REPORTED エントリの直後、ファイル末尾）に:

```yaml
  - kind: GOVERNMENT_CONFIRMED
    action_date: 2026-07-31
    known_at: 2026-08-03T00:00:00+00:00
    direction: JPY_BUY
    verified: false
    source_uri: https://www.mof.go.jp/public_relations/statement/other/20260803072806.html
    note: 財務大臣談話（8/3）で「米国東部時間 7 月 31 日、米国財務省と協調して円買い介入を実施した」と表明。談話ページの URL タイムスタンプは 07:28:06（JST と推定）。上界として東京 9:00 JST を置く。7/30 の単独介入は談話に無く公式表明なし
```

既存の REPORTED エントリ（同日のものを含む）は一切書き換えない。

注記（変更しない観察）: 2022-09-22 は REPORTED の known_at（14:59Z 上界）より
GOVERNMENT_CONFIRMED の known_at（09:34Z 実時刻）が早い。`intervention_risk_inputs`
は見えているイベントの最大段階を採るので、09:34Z〜14:59Z の間は
OFFICIAL_ACTION_CONFIRMED（0.8）だけが見える。公式表明は報道段階を含意するので
結果は正しく、REPORTED の上界を詰める作業はこの issue の範囲外。

## 変更 2: テスト（`tests/unit/`）

### 2-a. `tests/unit/test_intervention_collectors.py`（ローダー）

既存 `test_committed_episode_file_loads_and_maps`（同ファイル `:199`）は
`entries[0]` が REPORTED であることに依存しているので、エントリ追加後も先頭が
2022-09-22 の REPORTED のままなら変更不要（1-b の挿入位置を守れば先頭は変わらない）。

「Curated recognition timeline」セクションに 1 本足す:

```python
def test_committed_government_confirmed_entries_map_to_official_action_events():
    entries = load_episodes("config/intervention_episodes.yaml")
    confirmed = [e for e in entries if e.kind == "GOVERNMENT_CONFIRMED"]
    assert {e.action_date for e in confirmed} == {date(2022, 9, 22), date(2026, 7, 31)}
    for entry in confirmed:
        event = event_from_recognition(entry, FixedClock(RETRIEVED))
        assert event.event_type == "INTERVENTION_GOVERNMENT_CONFIRMED"
        assert event.known_at == entry.known_at
        # 同じ action_date の REPORTED と event_id が衝突しない（kind がキーに含まれる）
        reported = next(
            e for e in entries if e.kind == "REPORTED" and e.action_date == entry.action_date
        )
        assert event.event_id != event_from_recognition(reported, FixedClock(RETRIEVED)).event_id
        assert event.event_id == event_from_recognition(entry, FixedClock(RETRIEVED)).event_id
```

（`date` は既に import 済み。エントリ数を固定するアサーションは足さない。）

### 2-b. `tests/unit/test_intervention_collectors.py`（`intervention_risk_inputs` の段階と二重計上）

「Risk inputs」セクションに 1 本足す。既存の `_event` ヘルパーを使う:

```python
def test_government_confirmation_raises_stage_without_moving_recency():
    reported = _event(
        "INTERVENTION_REPORTED",
        {"action_date": "2026-07-31", "direction": "JPY_BUY", "verified": False},
        datetime(2026, 8, 1, 14, 59, tzinfo=UTC),
    )
    confirmed = _event(
        "INTERVENTION_GOVERNMENT_CONFIRMED",
        {"action_date": "2026-07-31", "direction": "JPY_BUY", "verified": False},
        datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
    )
    t = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    only_reported = intervention_risk_inputs([reported], t)
    both = intervention_risk_inputs([reported, confirmed], t)
    assert only_reported["verification_state"] == pytest.approx(0.6)  # MEDIA_CONFIRMED
    assert both["verification_state"] == pytest.approx(0.8)  # OFFICIAL_ACTION_CONFIRMED
    assert both["days_since_intervention"] == only_reported["days_since_intervention"]
```

`days_since_intervention` は最新 `action_date` からの経過日数で決まり（`features.py:56-64`）、
同日の GOVERNMENT_CONFIRMED を足しても `latest` は動かない。このテストでそれを固定する。
実装の変更は不要（調査済み）。

### 2-c. `tests/unit/test_feature_source.py`（PIT で見える範囲の最大段階）

`StoredFeatureSource.snapshot`（`src/trading/data/features.py:164-170`）は
`KIND_TO_STATUS` の全 kind を `known_before(now, kind, since)` で取り、
`intervention_risk_inputs` に渡す。ここが PIT ゲートなので、観測時刻によって
段階が切り替わることをこの層で固定する。`verification_state` だけに重みを置けば
`INTERVENTION_RISK` の値が段階そのものになる:

```python
def test_government_confirmation_becomes_visible_only_after_its_known_at():
    action = date(2026, 7, 31)
    reported = intervention_event(action, datetime(2026, 8, 1, 14, 59, tzinfo=UTC))
    confirmed = EventEnvelope(
        event_id=uuid4(),
        event_type="INTERVENTION_GOVERNMENT_CONFIRMED",
        source="TEST",
        payload={"action_date": action.isoformat()},
        retrieved_at=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
        known_at=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
    )
    stage_only = InterventionRiskConfig(version="test", weights={"verification_state": 1.0})
    source = StoredFeatureSource(
        FakeObservationRepository(),
        FakeEventRepository([reported, confirmed]),
        stage_only,
        InMemoryFeatureStore(),
    )

    before = source.snapshot(datetime(2026, 8, 2, tzinfo=UTC))
    after = source.snapshot(datetime(2026, 8, 4, tzinfo=UTC))

    assert before[f.INTERVENTION_RISK] == pytest.approx(0.6)  # MEDIA_CONFIRMED
    assert after[f.INTERVENTION_RISK] == pytest.approx(0.8)  # OFFICIAL_ACTION_CONFIRMED
```

（ruff の `line-length = 100` を超えないよう、長いアサーションは変数に受ける。）

（`intervention_event` ヘルパー・`FakeObservationRepository`・`FakeEventRepository`・
`InMemoryFeatureStore`・`InterventionRiskConfig`・`EventEnvelope`・`uuid4` は同ファイルで
import 済み。`make_source` は固定の `WEIGHTS` を使うので、ここでは直接組み立てる。）

## 影響しないことの確認（調査済み。変更しない）

- `src/trading/backtest/intervention_event_study.py:54` の `EVENT_TYPE` は
  `INTERVENTION_REPORTED` 固定で、`load_episodes_from_events`（`:161`）はその
  event_type だけを選ぶ。GOVERNMENT_CONFIRMED イベントは無視される
- `src/trading/backtest/shock_trigger_study.py:672` も同じ `EVENT_TYPE` で
  `known_before` する。影響しない
- `tests/unit/test_file_encoding.py` は `load_episodes()` をサブプロセスで呼ぶだけ。
  YAML に非 ASCII があるのは既存どおりで、追加エントリも同じ扱い
- `StoredFeatureSource._replay_rows`（`src/trading/data/features.py:241-257`）は
  `KIND_TO_STATUS` の全 kind を列挙するので、collector 再実行後は
  GOVERNMENT_CONFIRMED の known_at が replay の change instant になり、
  `dataset_fingerprint` も変わる。これは意図した効果でコード変更は不要
  （PR 本文の運用手順に「再実行後は research replay の fingerprint が変わる」と書く）
- `tests/replay` / `tests/failure` / `tests/integration` に YAML のエントリ数や kind 集合を
  固定しているテストは無い（`rg` で確認済み）

## 変更対象ファイル

| ファイル | 変更 |
| --- | --- |
| `config/intervention_episodes.yaml` | 先頭コメント 3 行追記、GOVERNMENT_CONFIRMED 2 件追加 |
| `tests/unit/test_intervention_collectors.py` | テスト 2 本追加（2-a, 2-b） |
| `tests/unit/test_feature_source.py` | テスト 1 本追加（2-c） |

ソースコード（`src/`）の変更なし。マイグレーション無し。

## 完了条件（実行コマンド。すべて worktree ルートで）

```bash
.venv/bin/ruff check .
.venv/bin/pytest tests/unit tests/replay tests/failure -q
.venv/bin/python -c "
from collections import Counter
from trading.data.intervention.episodes import load_episodes
entries = load_episodes()
print(len(entries), Counter(e.kind for e in entries))
print([(e.kind, e.action_date.isoformat(), e.known_at.isoformat()) for e in entries if e.kind == 'GOVERNMENT_CONFIRMED'])
"
```

- ruff が無変更で通る
- pytest が green（`tests/unit/test_invariants.py` を含む）
- 3 つ目のコマンドで GOVERNMENT_CONFIRMED 2 件（2022-09-22 / 2026-07-31）を含む
  全エントリが読め、重複キー例外が出ない

## やらないこと

- 新しいコレクター・HTTP 取得・スキーマ変更（`migrations/`）
- 覆面介入への GOVERNMENT_CONFIRMED の推測追加、MARKET_SUSPECTED の追加
- `verification_state_level` の写像値・`VERIFICATION_ORDER` の変更
- `src/trading/data/intervention/episodes.py` / `features.py` の変更（変更不要と確認済み）
- `intervention_event_study.py` / `shock_trigger_study.py` / `policy_event_study.py` の変更
- 既存 REPORTED エントリの known_at・note の書き換え
- `docs/research/` の過去ノートの書き換え
- 既存テストのアサーションを緩めること

## 規約（AGENTS.md より転記）

- frozen モデル + 新しい値を返す。引数を破壊しない
- 検証はシステム境界（YAML 読み込み）だけ。内部関数に防御的分岐を足さない
- WHAT を説明するコメントは書かない。「なぜ」だけ docstring に。AI レビューの引用や
  「〜のために追加」のような文脈依存コメントを残さない
- テストデータに実在の個人名を使わない（YAML の note に機関名・役職名を書くのは可、
  個人名は書かない）
- 金額・数量・価格に float を使わない（本タスクでは該当なし）
- ruff（`pyproject.toml` の設定）に準拠。行長は既存テストファイルに合わせる
- **コミットしない**（コミット・push・PR は Claude 側が行う）
- 計画に書かれていない変更（周辺リファクタ・無関係な整形・追加の抽象化）はしない
