# 단계별 사용자 확인 가이드

> 이 문서는 **에이전트가 뭘 만들었는지가 아니라, 사람이 무엇을 눈으로 봐야 하는지**만 다룬다.
> `SPEC.md`(계약·통과 조건)와 `TEST-CHECKLIST.md`(사람이 할 일)에 흩어진 확인 항목을 실전용으로 압축했다.
> 단계가 진행될 때마다 이 문서도 같이 갱신한다.
>
> 실제로 검증하며 남긴 텍스트(버그 리포트, 확인 완료 메시지 등) 원문은 요약하지 않고 [verification-log.md](verification-log.md)에 단계별로 그대로 보관한다.
> 후보 내용 자체에 대한 의미론적 판단("이건 맞다/이렇게 정의해야 한다")은 [decisions-log.md](decisions-log.md)에 기록한다 — 채점기가 못 잡는, 사람만 할 수 있는 판단이다.

---

## 0. 지금 진행 상황

| # | 단계 | 상태 |
|---|---|---|
| — | A. 키·모델·`.env` 준비 | 진행 중 (모델은 `gemini-3.1-flash-lite`로 재확정, §10 D-2) |
| — | B. 팀의 테스트 문서·`gold.csv` 검토 | ✅ 완료 (팀이 정답 15개·함정 10개에 동의) |
| — | 토큰 사용량 로깅 인프라 | ✅ 실제 호출 기록 쌓이는 중 (`usage-report`) |
| 1 | 계약 목킹 | ✅ 완료 — Spring에 전달할 스키마·예시 확정 |
| 2 | 문서 파싱 | ✅ 완료 — 오프셋 인프라 실측 검증 통과 |
| 3 | VARIANT (규칙) | ✅ 완료 — G4·G5·G12·G13 완전 일치, N9 미포함, 오탐 0 |
| 4 | SYNONYM (LLM) | ✅ 완료 — 정밀도 1.00 (합격선 0.70), 첫 호출부터 통과 |
| 5 | HOMOGRAPH (LLM) | ✅ `gemini-3.5-flash`·`gemini-3.6-flash` 둘 다 G6("주문") 100%(각 5/5), 오탐 0. `3.7·3.8-flash`는 서버 과부하로 측정 불가. §10 D-14·D-15 |
| 6 | FastAPI 래핑 | ⬜ 미착수 |

---

## 이력 남기기 (`out/history/`)

**`extract`를 돌릴 때마다 자동으로 `out/history/`에 타임스탬프 붙은 스냅샷이 남는다** — 코드를 고치기 전후 결과를 나중에 비교할 수 있어야 하기 때문이다(예: 라운드로빈 샘플링 버그를 고치기 전/후 비교).

```bash
python -m app.cli extract --fixtures fixtures/ --out out/result.json
# → out/result.json 갱신 + out/history/20260909T025349.json 자동 생성
```

- **덮어쓰지 않는다** — 실행할 때마다 새 파일이 생긴다
- `out/history/`는 `.gitignore`에 있는 `out/` 아래라 **로컬에만 남고 git에는 커밋되지 않는다**
- 튜닝 루프(4단계)처럼 하루에도 여러 번 돌리면 그만큼 파일이 쌓인다 — 지금은 의도된 동작이다. 나중에 너무 많아지면 그때 정리 방법을 정한다

단계를 "통과했다"고 판단한 시점처럼 **의미를 부여해 표시**하고 싶으면 라벨을 붙여 한 번 더 남긴다:
```bash
python -m app.cli archive --label step3
# → out/history/20260909T025351__step3.json 생성 (자동 스냅샷과 별개로 추가됨)
```
통과 확인 자체는 `TEST-CHECKLIST.md` 맨 아래 "확인 이력" 표에 날짜·확인자·확인 내용을 같이 적는다.

---

## 1단계 — 계약 목킹 ✅

**만들어진 것**: [app/schema.py](../app/schema.py)(요청·응답 Pydantic), [app/main.py](../app/main.py)(`POST /extract` 고정 응답, `GET /health`)

**사람이 확인할 것 — 딱 하나**: **이 응답 JSON을 Spring 담당자에게 보내 스키마를 확정받는다.** 오늘 가장 급한 일이라고 TEST-CHECKLIST.md가 못 박아 뒀다. 코드가 맞는지는 서버가 이미 Pydantic `response_model`로 강제하므로(스키마가 틀리면 500이 난다) 사람이 다시 검사할 필요는 없다 — Spring 쪽이 "이 필드로 우리 화면을 만들 수 있는가"를 보는 것이 진짜 검토다.

확인 방법:
```bash
uvicorn app.main:app --reload
curl http://localhost:8000/health
curl -X POST http://localhost:8000/extract -H "Content-Type: application/json" -d '{...§3.1 형태...}'
```

---

## 2단계 — 문서 파싱 ✅

**만들어진 것**: [app/pipeline/normalize.py](../app/pipeline/normalize.py)(오프셋 엔진), [app/cli.py](../app/cli.py)의 `extract`/`verify-offsets`

**돌릴 명령**:
```bash
python -m app.cli extract --fixtures fixtures/ --out out/result.json
python -m app.cli verify-offsets --result out/result.json --fixtures fixtures/
```

**통과 조건 (SPEC.md §8-2)**: `verify-offsets`가 전부 통과 + **사람이 직접 고른 5개의 `snippet`이 원문과 글자 단위로 일치**. `verify-offsets`가 "통과"라고 말해도, 검증 코드 자체가 틀렸을 수 있으니 **최소 3개는 사람이 직접 원문과 대조**해야 한다 (TEST-CHECKLIST.md C-2).

**확인 절차 — 실제 `out/result.json`의 예로**:

```json
{
  "documentId": "d-001",
  "form": "이용 보류",
  "snippet": "정 상태 정책\n\n결제 실패가 3회 누적되면 계정을 **이용 보류** 상태로 전환한다. 이용 보류 상태에서는 콘텐츠 조",
  "charStart": 1024,
  "charEnd": 1029
}
```

1. `documentId: "d-001"`이 어느 파일인지 확인한다 — `fixtures/workspace.json`을 직접 열어봐도 되지만, 더 빠른 방법은:
   ```bash
   python -m app.cli docs --fixtures fixtures/
   ```
   이 명령이 `documentId`·부서·제목·파일 경로를 표로 보여준다 (`d-001 기획 구독 커머스 정책 정의서 docs/커머스_기획안.md`). `verify-offsets` 출력 자체도 이제 `d-001(구독 커머스 정책 정의서)`처럼 제목을 같이 찍어준다 — **다만 이건 CLI 전용 편의 기능이고, `result.json`(API 계약)에는 문서 제목 필드가 없다.** 계약에 필드를 추가하려면 Spring과 먼저 맞춰야 한다(TEST-CHECKLIST.md E 표)
2. 그 파일을 열어 `snippet`에 적힌 문장을 눈으로 찾는다 → [fixtures/docs/커머스_기획안.md:33](../fixtures/docs/커머스_기획안.md#L33)에 실제로 있다
3. **스니펫 문자열이 원문과 토씨 하나 안 틀리고 같은지**만 보면 된다 — 다르면(오타·다른 문장) 오프셋 계산이 틀렸다는 뜻
4. `warnings` 필드도 확인 — 지금은 정직하게 "판정 로직 없음"이라고 적혀 있어야 한다

**지금 result.json에서 확인할 게 아닌 것**: `candidates`가 2개뿐이고 재현율이 낮은 것 — 3~5단계 판정 로직이 아직 없어서 정상이다. 지금 볼 건 오직 **"있는 occurrence의 좌표가 정확한가"**.

---

## 3단계 — VARIANT (규칙, LLM 없음) ✅

**만들어진 것**: [app/pipeline/variant.py](../app/pipeline/variant.py) — §4의 정규화 규칙(공백 제거 → 하이픈·언더스코어 제거 → 소문자화 → 조사 제거)만으로 그룹을 짓는다. LLM을 쓰지 않는다.

**후보를 어떻게 찾아내는가**: 문서의 마크다운 강조(`**볼드**`)와 코드(`` `백틱` ``) 스팬을 후보 표기로 본다 — 형태소 분석기·임베딩 없이, 문서 작성자가 이미 강조해 둔 곳을 이용하는 규칙이다. 정규화 결과가 같은 표기가 2개 이상이면 그룹으로 묶는다.

**돌릴 명령**:
```bash
python -m app.cli extract --fixtures fixtures/ --out out/result.json
python -m app.cli verify-offsets --result out/result.json --fixtures fixtures/
python -m app.cli score --result out/result.json --gold fixtures/gold.csv
```

**통과 조건 (SPEC.md §8-3)**: 골드셋 `G4`(로그인/로그 인) `G5`(결제 수단/결제수단) `G12`(회원 가입/회원가입) `G13`(e-mail/email/E-Mail)을 **전부** 찾고, **`N9`(결제일/결제월)를 절대 묶지 않는다.** 오탐 0이 목표.

**실제 결과**:
```
=== 그룹형 (VARIANT · SYNONYM) ===
완전 일치   4 / 11     G4 G5 G12 G13
부분 일치   G11  (누락: 계정 공유 차단, 기기 제한)
누락        6 / 11     G1 G2 G3 G8 G9 G10
오탐        0

정밀도  1.00   재현율  0.45
```
`G4 G5 G12 G13`이 전부 완전 일치, 오탐 0 — **3단계 통과 조건 충족.** `G1~G3 G8~G10`(SYNONYM)이 누락으로 뜨는 건 정상이다 — 그건 LLM이 필요한 4단계 몫이다. `G11`이 "부분 일치"로 뜨는 건, 마침 VARIANT 규칙이 API 필드명 표기 변형(`maxSession`/`max_session`)을 하나 더 찾아냈는데 그 표기가 우연히 G11(SYNONYM 세 표기 중 하나)에도 포함돼 있어서다 — SYNONYM 자체를 찾은 건 아니니 무시해도 된다.

**확인 절차**:
1. `score` 출력의 "그룹형" 표에서 `G4 G5 G12 G13`이 "완전 일치" 줄에 있는지 확인
2. **`N9`가 오탐 목록에 나오면 안 된다** — 위 결과처럼 "오탐 0"이어야 한다. 나온다면 §4의 VARIANT 규칙(편집거리 제거)이 제대로 구현 안 된 것. `SPEC.md` §10 D-1(편집거리를 왜 뺐는지)을 다시 읽어보면 이 함정의 의미를 알 수 있다
3. `verify-offsets`로 이번에 새로 생긴 occurrence(총 24개)도 전부 통과하는지 확인 — VARIANT가 새 occurrence를 만들어내므로 2단계 때처럼 다시 대조가 필요하다
4. 오탐이 하나라도 있으면 이 단계는 통과가 아니다 — LLM이 없는 순수 규칙 단계라 오탐이 나오면 규칙 자체가 틀린 것이니 즉시 고쳐야 한다

---

## 4단계 — SYNONYM (LLM 첫 사용) ✅

**만들어진 것**: [app/pipeline/llm.py](../app/pipeline/llm.py)(Gemini 호출·캐시·카운터·재시도), [app/pipeline/synonym.py](../app/pipeline/synonym.py)(LLM 결과 → 실제 오프셋 채운 Candidate 변환), [prompts/01_role.md~04_input.md](../prompts/)

**통과 조건 (SPEC.md §8-4)**: **정밀도 0.70 이상.** 재현율은 참고만.

**실제 결과**: **정밀도 1.00 — 첫 호출부터 합격선을 넘어서, `02_forbidden.md`에 규칙을 단 한 줄도 추가하지 않고 통과했다.** (자세한 시도 기록은 `SPEC.md` §10 D-4·D-6)
```
완전 일치   4 / 11     G4 G5 G12 G13 (VARIANT, 3단계와 동일)
+ 매 호출마다 SYNONYM 2~3개 추가로 발견 (예: 마일리지/혜택금, 상세 보기/detail_load, 이용 보류/suspend_account 등)
오탐        0
정밀도  1.00   재현율  0.64 (3회 평균)
```

**확인 절차**:
1. `score` 출력에서 "정밀도" 숫자를 **매번 기록**한다 (AGENTS.md) — 이번엔 처음부터 1.00이라 `02_forbidden.md` 튜닝 루프 자체가 필요 없었다
2. **`out/result.json`에서 새로 생긴 `kind: "SYNONYM"` 후보들을 직접 읽어본다** — `reason`이 실제로 말이 되는지, `proposedPreferredForm`이 억지스럽지 않은지 사람이 판단. 호출마다 정확히 같은 조합이 나오지 않는다(예: 어떤 호출은 `이용 보류/suspend_account`를 찾고 어떤 호출은 못 찾음) — **찾은 것 중에 틀린 게 없는지**가 핵심이지, 매번 같은 걸 찾아야 하는 건 아니다
3. **처음 Gemini를 실제로 부른 단계이므로 토큰 사용량 확인**:
   ```bash
   python -m app.cli usage-report --today
   ```
   실측: 문서 5장 기준 입력 5,374~5,615 토큰, 출력 641~802 토큰 — SPEC.md §5가 분할 호출이 필요하다고 보는 기준(20만 토큰)의 3%도 안 된다

---

## 5단계 — HOMOGRAPH ✅ 완료

**통과 조건 (SPEC.md §8-5)**: 골드셋 `G6`("주문")을 찾고 `senses`가 **정확히 2개**. **데모 핵심이라 필수.** `G7`(활성) `G14`(정산) `G15`(등급)는 참고 지표.

**시행착오**: `gemini-3.1-flash-lite`(7회)·`gemini-3.5-flash-lite`(5회) 둘 다 "주문"을 안정적으로 못 찾았다(각각 0%·20% 성공률, 후자는 오탐도 재발) — 자세한 과정은 `SPEC.md` §10 D-7·D-9·D-10·D-12·D-13. 프롬프트 문제가 아니라 **Lite 등급 모델의 판단 한계**로 보고, Lite가 아닌 `gemini-3.5-flash`로 교체(D-14) — 단 이 등급은 **무료 티어 RPD가 20회뿐**이라 신중하게 5회만 재현성 확인.

**최종 결과 — `gemini-3.5-flash`, 5회 호출:**

| 호출 | G6(주문) | G7(활성) | G14(정산) | G15(등급) | 그룹형 오탐 |
|---|---|---|---|---|---|
| 1 | ✅ | ✅ | ❌ | ✅ | 0 |
| 2 | ✅ | ✅ | ✅ | ✅ | 0 |
| 3 | ✅ | ✅ | ✅ | ✅ | 0 |
| 4 | ✅ | ✅ | ✅ | ✅ | 0 |
| 5 | ✅ | ✅ | ✅ | ✅ | 0 |

**`G6`("주문") 5/5(100%), 오탐 0/5.** `verify-offsets`도 매번 전부 통과. §8-5 통과 조건을 신뢰할 수 있는 수준으로 충족했다(§10 D-14).

**남은 트레이드오프**: `gemini-3.5-flash`의 RPD 20은 개발·튜닝 단계에는 충분하지만, 실서비스에서 하루 여러 요청을 처리하기엔 부족할 수 있다 — 유료 등급 전환 여부는 팀이 검토.

**확인 절차**:
1. `out/result.json`에서 "주문"·"등급"의 `senses`가 실제로 말이 되는지 확인 (`SPEC.md` D-14에 실제 reason 인용)
2. 데모에 이 결과를 그대로 쓸지 팀과 상의 (TEST-CHECKLIST.md D 표)
3. 실서비스 전환 시 RPD 20 한도로 충분한지 팀이 재검토

---

## 6단계 — FastAPI 래핑 ⬜

**통과 조건 (SPEC.md §8-6)**: CLI와 같은 결과를 HTTP로 반환. `/health` 동작.

**확인 절차**:
1. `python -m app.cli extract`로 만든 `out/result.json`과, 같은 fixtures로 `POST /extract`를 호출한 HTTP 응답을 **나란히 비교** — candidates 내용이 같아야 한다
2. 이 단계에서 비로소 `app/main.py`의 1단계 고정 목업이 실제 파이프라인 호출로 교체된다 — Spring에 다시 한번 "이제 진짜 값이 나온다"고 알려야 한다

---

## `gold.csv` 읽는 법

헤더: `id,kind,forms,preferred_form_hint,english_hint,trap_type,note`

- `id`가 **`G*`면 찾아야 하는 정답**, **`N*`면 찾으면 오답**(함정)
- `forms`는 `|`로 구분된 표기들
- `score` 명령은 각 `G*` 행의 `forms` 집합과 candidate의 `forms` 집합이 얼마나 겹치는지로 **완전 일치**(전부 겹침)·**부분 일치**(일부만)·**누락**(전혀 안 겹침)을 가른다. `N*` 행은 겹치면 무조건 **오탐**이다
- 정밀도 = (완전 일치 + 부분 일치) ÷ (완전 일치 + 부분 일치 + 오탐), 재현율 = (완전 일치 + 부분 일치) ÷ 전체 `G*` 행 수 — `app/scoring.py`의 `compute_score()` 참고

---

## 토큰 사용량(`usage-report`) 읽는 법

로그 위치: `logs/llm_usage.jsonl` (한 줄 = 호출 한 건, `.gitignore`에 있어 커밋 안 됨). **4단계 전까지는 비어 있는 게 정상이다.**

```bash
python -m app.cli usage-report            # 지금까지 전체 누적
python -m app.cli usage-report --today    # 오늘 것만
```

예시 출력(4단계 이후, 아래는 형식을 보여주기 위한 예시일 뿐 실제 기록 아님):
```
총 호출 2  (캐시 히트 1 · 실제 API 호출 1)
입력 토큰 합계 10,000  ·  출력 토큰 합계 1,400
stage별 호출 수: synonym: 1, homograph: 1

무료 티어 한도(Gemini 2.0 Flash-Lite): RPD 1500회 · RPM 30회 · TPM 1,000,000
오늘 실제 API 호출: 1 / 1500  (캐시 히트는 한도를 소모하지 않음)
```

이 숫자로 할 일 (TEST-CHECKLIST.md C-4):
- **비용**: 입력·출력 토큰 합계를 Google AI Studio 콘솔의 현재 단가와 곱해 1회·하루 비용 계산 (단가는 자주 바뀌어 코드에 하드코딩하지 않았다)
- **오늘 얼마나 더 돌릴 수 있나**: `오늘 실제 API 호출`을 화면에 같이 나오는 `RPD` 한도와 비교 (캐시 히트는 한도를 안 먹으므로 제외됨)
- **어디서 호출을 많이 쓰는지**: `stage별 호출 수`로 SYNONYM/HOMOGRAPH 중 어느 쪽이 튜닝 루프에서 API를 많이 쓰는지 확인

---

## 막혔을 때 — 에이전트가 임의로 정하면 안 되는 것

TEST-CHECKLIST.md E 표 요약. 아래 상황이면 **에이전트에게 판단을 맡기지 말고 사람이 정한다**:

| 증상 | 사람이 정할 것 |
|---|---|
| 골드셋 정답이 이상해 보인다 | `gold.csv`를 고치게 두지 않는다 — 팀이 판단 |
| 정밀도가 세 번 시도 후에도 미달 | 후퇴선(경고 등급)을 발동할지 |
| 컨텍스트 한계 초과 | 문서를 줄일지, 부서별로 나눠 호출할지 |
| 계약에 없는 필드가 필요해 보인다 | Spring 담당과 함께 결정 |
| 의존성을 더 넣고 싶다고 한다 | 이유를 듣고 판단. `SPEC.md` §9 목록이 기본 |
| 임베딩 클러스터링·형태소 분석기를 넣으려 한다 | 채점으로 재현율 부족이 확인되기 전에는 막는다 |
