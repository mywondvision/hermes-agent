# Serving lifecycle: допуск и проверка host binding

2026-11-09. Исполнитель: текущий интегратор Hermes, сессия `01a1b1c5-d042-7a41-9a2b-cbf70b8b3495`.

## Основание и текущая граница

- Канонический CR: `docs/change-requests/CR-hermes-symphony-serving-lifecycle-20261106.md`, normalized revision `c28c4ba9bc9b3b3deb70d61469231da65f5aef3ab7bd468d340f87e55c6e49d1`.
- Независимый review PASS и gate READY: `.artifacts/voice-integration-20261109/serving-review-final.json`, `serving-context-pack.json`, `serving-review-pack.json`. Исходный отчёт не изменяется.
- Решение владельца: сообщение `ок` непосредственно после итогового отчёта интегратора, предлагавшего `ok` на этот CR и CODE scope. Это разрешение реализации описанного дизайна. Прежние запреты, бюджеты и отдельные условия не обнуляются.
- Оно не является разрешением no-model E1/E2 campaign, transport pilot, model/native sessions, микрофона, передачи речи, paid resources, production, установки, push или merge.
- Проверенный HEAD `8169f1b66a3a7c6844e3347c89f5cfa82edd4483`, ветка `codex/hermes-symphony-cancel-correction-20261106`, этот worktree. Ранее изменённые файлы сохраняются, индекс не используется.
- Доступный app-server inventory: интегратор active, прежние serving/live sessions notLoaded. Это не доказательство отсутствия всех внешних writers. Другие сессии не возобновлялись; Bimo/kit worktree не изменялись.

## Результат первой необходимой проверки

**BLOCKING для рабочего serving bootstrap; реализация зависимого пути HOLD по пункту 1 самого CR.** У существующего plugin есть безопасные регистрация и unload, но не найден достаточный production binding, который одновременно задаёт serving lifetime, профиль и проверенный request-scoped PM context.

Это не утверждение, что у Hermes вообще нет lifecycle hooks. Проверены конкретные кандидаты:

| Точка | Что действительно даёт | Чего не доказывает |
|---|---|---|
| `PluginLoaderMixin._load_plugin_scoped` / `register(ctx)` | Загрузку в профиле owning manager, ledger регистрации | Что процесс обслуживает разговоры. Discovery вызывается также из `model_tools` import и `plugins_cmd` toolset resolution |
| `run_with_load_deadline` | Ограничение времени import/register; abandoned worker и отказ от поздних регистраций | Serving lifetime и безопасность произвольных OS ресурсов, созданных abandoned кодом вне ledger |
| `PluginContext.on_unload`, `PluginLedgerMixin.unload` | Cleanup в обратном порядке при unload/rediscovery | Готовность serving host до admission; request authority; гарантированный stop после crash |
| `gateway.hooks` / `gateway:startup` | Отдельный hook registry, gateway event | Общую plugin lifecycle-точку для Desktop/TUI/CLI; обязательный fail-closed bootstrap. Ошибки handlers логируются, выполнение продолжается |
| `PluginContext.session_cwd/session_surface`, `plugin_session.session_key` | Маршрутизацию и поверхность текущей сессии | Grant/contract/payload digest для PM-запроса. Такие значения не разрешают native execution |
| `pre_tool_call` | Возможность block/modify аргументы, call/session IDs | Доверенный PM grant. Аргументы принадлежат модели; hook failure обрабатывается отдельно и не создаёт authority |
| `symphony_pm.wire` | Явную in-process injection port/context_source в loaded module | Сам host binding. Production caller с таким context_source в текущем tracked source не найден; тесты выполняют injection вручную |

Не заменяем этот пробел стартом исполнителя при любом discovery, session-start lazy bootstrap, синтетическим контекстом из текущих tool args, или hardcode Symphony в gateway/core. В CR прямо записано: «отсутствие подходящей seam — HOLD с отдельной generic extension, не hardcode Symphony в core».

Наличие callback API само по себе не означает, что соединение невозможно. Установлено более узкое: в проверенном исходном дереве нет достаточной готовой привязки с требуемыми гарантиями. Требуется выбрать и согласовать generic host extension; реализовывать journal/runner с недостижимым consumer вместо этой привязки сейчас не предлагается.

## Минимальная следующая дельта (PROPOSED, не дополнительное разрешение)

Продолжение в той же линии CR, без новой очереди/HTTP/dispatcher/runtime:

1. Generic profile-owned service registration: `register(ctx)` только объявляет service factory. Подтверждённый serving host запускает её после owning-profile discovery и до новых conversations/admission. Diagnostic/discovery команды не захватывают owner lock и не создают journal/Job. Обязательная startup failure оставляет потребителя UNAVAILABLE/HOLD.
2. Host-owned request binding: отдельный проверенный issuer/approval source связывает immutable profile/session/request digest/grant/contract с одним tool request. Plugin читает его только в текущем request scope; вне scope — None/HOLD. Call/session IDs не считаются grant. Передача context через tool args/config boolean/модель запрещена. Конкретный approval source и его связь с native permit должны быть определены до реализации, а не выведены из этого `ок`.
3. Нормальное завершение: host закрывает admission первым, вызывает bounded drain и ledger cleanup в owning profile. Plugin unload/force rediscovery не должны оставлять старый service доступным; недоступный cross-process live port при pinned host isolation остаётся отказом.
4. Первый consumer — существующий `symphony_pm`; один lifecycle/journal/port на profile+namespace. Kit driver и frozen S2 неизменны. Не добавлять API без consumer.

Предлагаемая область файлов для будущего согласования: generic sibling(s) `hermes_cli/plugin_services.py` / request-binding module, узкие additive методы facade `hermes_cli/plugins.py`, owning-host вызовы в `tui_gateway` и `gateway`, plugin consumer `plugins/symphony_pm`, контрактные тесты и docs. Эти имена — предложения, не утверждение о существующих файлах или окончательном API. Root/area AGENTS необходимо прочитать перед каждой правкой.

Критерии: discovery без serving не создаёт ресурсы; два профиля не делят authority/handles; request scope теряется после возврата/в постороннем потоке; отсутствующий/просроченный/mismatched grant отказывает; bootstrap/reload/shutdown закрывают admission; старые plugins продолжают работать; tool/prompt surface не меняется посреди разговора. Дальше применяются исходные journal/launch/cancel crash criteria CR.

## Budget / следующий checkpoint

Новый material host/authority contract не включён молча в прежний Context Pack. Единственное ранее выданное дополнительное разрешение на metadata correction/review уже использовано. CR и review не переписываются, proof budget не сбрасывается.

Нужен ограниченный scope decision владельца: включить generic host binding в тот же CR и разрешить один независимый проход изменённого контракта. После этого — gated candidate для реализации, затем отдельно разрешённая no-model campaign. До этого source implementation и runtime не меняются.

## Состояние результата

- CODE approval существующего CR: получен.
- Host seam qualification: E0, blocking finding сохранён; не runtime failure и не общий отрицательный вывод о Hermes.
- Source implementation: NOT_STARTED из-за обязательного HOLD пункта 1.
- Tests/native/model/runtime replay: NOT_RUN; прежние matching component evidence не переисполнялись.
- Global key / живой голос / installed serving / общий cancel: прежние статусы без повышения.
- Публикация/установка/production: не выполнялись.
