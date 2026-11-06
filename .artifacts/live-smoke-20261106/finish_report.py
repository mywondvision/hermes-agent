"""Evidence packaging only. Never starts a model, worker, shell, or network call."""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


hold = read(HERE / "native-campaign-hold.json")
baseline = read(HERE / "baseline.json")
manifest = read(ROOT / ".artifacts/frozen-s2/snapshot-manifest.json")
frozen = []
for row in manifest["files"]:
    content = (ROOT / ".artifacts/frozen-s2" / row["path"]).read_bytes()
    frozen.append({"path": row["path"], "size_matches": len(content) == row["bytes"],
                   "sha256_matches": hashlib.sha256(content).hexdigest() == row["sha256"]})
assert all(r["size_matches"] and r["sha256_matches"] for r in frozen)
write(HERE / "frozen-readback.json", {"status": "PASS", "files": frozen})
write(HERE / "first-verdict.json", {"status": "FAIL_CONFIG_BEFORE_MODEL_SESSION",
      "request": hold["request"], "job": hold["job"], "attempt": hold["attempt"],
      "result_json_received": False, "native_attempts": 1, "hold": True})
write(HERE / "cancel-verdict.json", {"status": "NOT_RUN_HOLD", "native_attempts": 0,
      "native_cancel_calls": 0, "reason": "First read-only JSON result missing; no automatic retry",
      "worker_stopped": "UNKNOWN", "descendants_stopped": "UNKNOWN",
      "external_effects_settled": "UNKNOWN", "billing_settled": "UNKNOWN"})
def log_text(path):
    raw = path.read_bytes()
    return raw.decode("utf-16" if raw.startswith(b"\xff\xfe") else "utf-8-sig")


offline = log_text(HERE / "offline.txt")
regression = log_text(HERE / "regressions.txt")
assert "90 tests passed, 0 failed" in offline
assert "54 tests passed, 0 failed" in regression
write(HERE / "summary.json", {"offline_contract": "PASS_144_OF_144",
      "live_success": "FAIL_CONFIG_BEFORE_MODEL_SESSION", "live_cancel": "NOT_RUN_HOLD",
      "native_attempts": 1, "native_cancel_calls": 0, "automatic_native_retries": 0,
      "mechanical_process_probe": "PASS_WITHOUT_MODEL", "frozen_files_verified": len(frozen),
      "reservation": "POSSIBLY_CONSUMED_HELD", "billing_settled": "UNKNOWN"})
argv = read(HERE / "attempt-1/command.json")["argv"]
quote = lambda s: "'" + s.replace("'", "''") + "'"
exact = "Get-Content -Raw -Encoding UTF8 " + quote(str(HERE / "attempt-1/prompt.txt")) + " | & " + " ".join(map(quote, argv))
(HERE / "executed-native-command.txt").write_text(
    "ARCHIVE ONLY. The temporary projected schema was cleaned by the existing driver.\n"
    "This is the single executed native attempt, not an instruction to retry.\n\n" + exact + "\n", encoding="utf-8")
report = f"""# Hermes → Symphony / Codex: ограниченный smoke, 2026-11-06

Итог: **HOLD**. Офлайн-контракт принят свежими целевыми проверками; успешный живой JSON не получен, живой cancel не запускался. Использована **1 native attempt из допуска максимум 2**; automatic native retry = 0. Второй прогон удержан по условию проверки первого.

| Контур | Фактический результат |
|---|---|
| Офлайн-контракт / registry / toolsets | **PASS 144/144**, canonical runner, retries 0 |
| Реальный Codex process | Один запуск, **exit 1**, до model session; JSON отсутствует |
| Успешный live worker | **FAIL_CONFIG_BEFORE_MODEL_SESSION** |
| Живой cancel, lost response, cancel redelivery | **NOT_RUN / HOLD** |
| OS process-tree probe без модели | PASS; отдельный query-only observer, 5 известных процессов/launcher identities |
| Повтор submit после reopen failed-attempt journal | DUPLICATE, число native tickets 1 → 1, новых процессов нет |
| Billing / external effects | **UNKNOWN**, reservation удерживается |

Авторизация: текущий запрос на минимальный in-process binding и два синтетических прогона, затем ответ «Да, согласую два запуска» для GPT‑6.1 Sol High / текущего Windows. Исходный offline CR и его approval не переписаны. Текущая дельта живёт только в task-local `.artifacts/live-smoke-20261106/` и этом evidence package.

Версии и источники:

- Hermes HEAD: `{baseline['hermes_head']}`; PM contract `hermes-pm-ingress/v1`.
- Существующий kit worktree: `D:/AI/worktrees/kit-wave1-integration-20261031`, VERSION `{baseline['kit_version']}`, HEAD `{baseline['kit_head']}`. Загружен реальный `runtimes/codex/orchestration/driver.py`, SHA256 `1b88924648b442675749890b726668790f8c7864707823e06f543acdd0cbe176`.
- Сверена October qualification copy `D:/Codex/2026-10-31/task/symphony-worktree`: driver SHA256 `fd5a70d5ea63b3da61141c25b9e270cc2e2eb61843e9dc008c9b408ec338d4b1`, без актуальной effort-поддержки выбранной копии. Старый `D:/Claude/b24-universal-kit` имеет VERSION `0.15.2`, этого driver нет. Installed integrator plugin location — `0.16.8`. Никакие внешние исходники не изменены; latest upstream по сети не объявляется проверенным.
- Codex CLI **0.167.0**, executable и SHA256 в `baseline.json`; help/readiness `ready` сохранены в `preflight.json`. Выбор: `gpt-6.1-sol`, `model_reasoning_effort=high`.
- Existing test Python: `D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe`, Python **3.14.9**, pytest **9.1.1**, psutil **7.2.2**. Новая Python/OS/model environment не создавалась.

Binding: real PluginManager → plugin `wire()` → real registry handler → `LivePort` → один SQLite owner journal → `CodexDriver.launch_review()` → injected Windows process runner. Используются транзакции/operation dedupe существующего reference Owner; новая таблица launches — spent launch tickets в той же БД, без runnable queue и scheduler loop. Reference `Owner.admit`, fake sink, synthetic terminal proof и S2 PRIMARY не вызываются. Это task-local live binding к исполнителю, не развёрнутый Symphony scheduler и не production transport.

Подготовлены два вложенных синтетических Git repository без remote: `workspace-1`, `workspace-2`, только новые `input.json` и `AGENTS.md`. Worker request read-only; отдельная простая JSON schema. Рабочие проекты в prompt/payload не передавались. CLI `--ignore-user-config`, read-only sandbox, ephemeral, web_search disabled, multi_agent false; существующая авторизация, без новых ключей.

Windows runner создаёт root suspended, присоединяет к non-breakaway kill-on-close Job Object до resume, сохраняет exact PID/creation FILETIME в owner target. Наблюдатель запускается отдельным Python-процессом с query-only Job handle и synchronize/query Process handles. Основы: [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects), [TerminateJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject). Это process-lifetime handle, не durable handle service после controller crash. Connection reopen не называется полным controller restart. Таймаут 180 секунд — operational watchdog, не денежный лимит и не доказательство stop.

Идентичность единственного реального exec:

- request: `{hold['request']}`
- job: `{hold['job']}`
- job revision: `a659a205e03e87d86f3e397cec383f079ab7f75b5a93035a72cd5ec346c21568`
- attempt: `{hold['attempt']}`
- process: **33568**; creation FILETIME **134384567844359066**; native thread/session ID **не создан**. Поле `session` в раннем binding — controller attempt placeholder, не доказанный Codex thread ID.
- launch ticket один; `NATIVE_STARTED` event один; `command.json` один; durable terminal state `FAILED_CONFIG_HOLD`; native cancel effect tickets **0**.

Непосредственный блокер: добавленные harness параметры `model_providers.openai.request_max_retries=0` / `stream_max_retries=0` отвергнуты установленным CLI:

> model_providers contains reserved built-in provider IDs: `openai`. Built-in providers cannot be overridden.

`attempt-1/stderr.txt`, `driver-result.json`, `stdout.jsonl` и `command.json` сохраняют фактический результат. stdout пуст, `thread.started`/`turn.started` отсутствуют, JSON result отсутствует. Изменять provider/channel в обход этого отказа не стали. Неверные override удалены из подготовленного кода; `debug models --bundled` с исправленным config прошёл без model call/refresh. Исправленный `exec` **не запускался**. Отключение внутренних HTTP/SSE retry в этой версии не квалифицировано; лимит native attempts контролируется отдельно.

До реального exec была одна локальная ошибка `RECEIPT_STALE`: receipt wall time на доли секунды опережал immutable context.now. В этот момент native boundary не вызывался: в attempt folder был только prompt, command/binding отсутствовали, Job Object отсутствовал, dispatch ticket сохранялся. Readback в `pre-native-reconciliation.json`. Исправлен shared request tick и добавлен барьер PM receipt → native start. Продолжен тот же никогда не запущенный ticket, без его сброса. Затем ровно один реальный exec завершился отказом config; он не повторялся.

После exit 1 controller увидел Job active=0, затем закрыл handle. Отдельный последующий readback подтвердил PID отсутствует (OpenProcess error 87) и named Job отсутствует (OpenJobObject error 2). Полное независимое terminal proof всех потомков для этого failed attempt **UNKNOWN**, поскольку Job handle уже закрыт. Это не объявляется проверкой cancel или полным PASS. Для отдельного OS supervision probe query-only observer успел подтвердить все известные process handles signaled и Job active=0; этот probe не подменяет живой cancel Codex.

Команды из cwd `D:/AI/worktrees/hermes-pm-ingress-20261031` (PowerShell):

```powershell
& ./.artifacts/live-smoke-20261106/offline.ps1
& ./.artifacts/live-smoke-20261106/regressions.ps1
$smokePython = 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe'
& $smokePython -B .artifacts/live-smoke-20261106/smoke.py prepare
& $smokePython -B .artifacts/live-smoke-20261106/smoke.py process-check
& $smokePython -B .artifacts/live-smoke-20261106/smoke.py binding-check
& $smokePython -B .artifacts/live-smoke-20261106/smoke.py run
& $smokePython -B .artifacts/live-smoke-20261106/smoke.py reconcile-unstarted
& $smokePython -B .artifacts/live-smoke-20261106/smoke.py continue-unstarted
& $smokePython -B .artifacts/live-smoke-20261106/smoke.py finalize-failed-native
& $smokePython -B .artifacts/live-smoke-20261106/smoke.py config-check
& $smokePython -B .artifacts/live-smoke-20261106/smoke.py recovery-check
& $smokePython -B .artifacts/live-smoke-20261106/smoke.py inspect
```

Это журнал выполненного, не список для повторного исполнения: campaign.once / continue-unstarted.once / native-campaign-hold.json блокируют новые exec. Полный фактический native argv — `executed-native-command.txt` и `attempt-1/command.json`; временная projected schema удалена штатным `driver.cleanup`.

Canonical targeted commands внутри двух `.ps1` используют только уже установленный test interpreter и `scripts/run_tests.sh`, `-j 1 --file-retries 0 -v`: первые четыре файла дали 90/90, registry/toolsets ещё 54/54. Runner работал вне sandbox после свежего доказанного Git Bash CreateFileMapping error внутри sandbox. Полные логи `offline.txt`, `regressions.txt`. Runner обновляет ignored `test_durations.json` cache штатно. Ruff harness — PASS. Полный repository aggregate не повторялся: production/source diff отсутствует, предыдущий aggregate имел известные pre-existing artifact findings; полный CI PASS не заявляется.

Сохранность: все baseline source hashes, external driver hashes, Hermes HEAD/tracked status совпадают; frozen S2 **{len(frozen)}/{len(frozen)}** sizes/hashes совпадают. Исходные adapter/plugin/tests/reference owner не редактировались. Без Linear writes, push, merge, новых credentials, постоянного доступа или production изменений. Synthetic state/workspaces/results не удалены.

Недостающий результат — успешно завершённая read-only Codex attempt с проверенным JSON. Нужен отдельный owner direction на повтор после исправления CLI options; automatic retry запрещён текущим запросом. До этого второй native cancel campaign не открывается. Контракт отмены и OS supervision квалифицированы только в перечисленном офлайн/безмодельном объёме; live lost-response/redelivery/independent descendants stop остаются NOT_RUN/UNKNOWN/HOLD.
"""
(HERE / "REPORT.md").write_text(report, encoding="utf-8")
out = ROOT / "docs/audits/CR-hermes-symphony-ingress-offline-20261106/live-smoke-20261106"
out.mkdir(exist_ok=True)
(out / "REPORT.md").write_text(report, encoding="utf-8")
write(out / "summary.json", read(HERE / "summary.json"))
print(json.dumps(read(HERE / "summary.json"), indent=2))
