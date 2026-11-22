"""Isolated personal TASK peer. Launch only in an approved synthetic campaign."""

import asyncio
import base64
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from threading import Event
import time

from tests.plugins.symphony_forward_peer import ForwardPeer
from tests.plugins.symphony_plan_peer import run


def _public_key(value):
    if type(value) is not str or len(value) != 44:
        raise ValueError('expected raw Ed25519 key in standard base64')
    raw = base64.b64decode(value, validate=True)
    if len(raw) != 32 or base64.b64encode(raw).decode('ascii') != value:
        raise ValueError('invalid Ed25519 key representation')
    return raw


def _diagnostic_error(error):
    from plugins.symphony_pm.transport.types import Refused

    reason = str(error)
    return {'error_type': type(error).__name__,
            'error_reason': _diagnostic_code(reason) if isinstance(error, Refused) else None}


def _diagnostic_code(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z_0-9]{1,95}', value) else None


class OperatorPeer(ForwardPeer):
    def __init__(self, *args, result_rights=()):
        super().__init__(*args)
        self.result_rights = result_rights
        self.worker_armed = Event()
        self.worker_waiting = Event()
        self.worker_released = Event()
        self.stale_stop_targets = 0
        self.worker_outcomes = {}
        self.plan_answers = []
        self.launch_handoffs = {}

    def inspect(self):
        # STOP counters are needed while the controlled worker still owns the attempt.
        return super().inspect(wait_for_completion=False)

    def _install_observers(self, patch):
        from hermes_state_runtime import RuntimeStoreError
        from plugins.symphony_pm import desktop_operator_stop

        super()._install_observers(patch)
        self._install_worker_diagnostics(patch)
        self._install_launch_handoff_diagnostics(patch)
        create = self.native.create_suspended
        check_target = desktop_operator_stop._check_target

        def count_stale_target(*args, **kwargs):
            try:
                return check_target(*args, **kwargs)
            except RuntimeStoreError as error:
                if error.reason == 'stale_stop_target':
                    with self.counter_lock:
                        self.stale_stop_targets += 1
                raise

        patch.setattr(desktop_operator_stop, '_check_target', count_stale_target)

        def controlled_create(*args):
            receiver = create(*args)
            if not self.worker_armed.is_set():
                return receiver
            self.worker_armed.clear()
            self.channel.owner.observer.stopped = False
            wait, cancel = receiver.wait_result, receiver._cancel

            def wait_result(*values, **kwargs):
                self.worker_waiting.set()
                try:
                    if not self.worker_released.wait(45):
                        raise AssertionError('TASK fixture worker was not released')
                    return wait(*values, **kwargs)
                finally:
                    self.worker_waiting.clear()

            def cancel_and_release():
                result = cancel()
                self.worker_released.set()
                return result

            receiver.wait_result = wait_result
            receiver._cancel = cancel_and_release
            return receiver

        patch.setattr(self.native, 'create_suspended', controlled_create)

    def _install_worker_diagnostics(self, patch):
        from plugins.symphony_pm import desktop_route

        execute = self.channel.owner.execute_reserved
        decide = desktop_route.decide_presented

        def observe_execution(token, ticket, request):
            row = dict(attempt_id=ticket.attempt, state='running', error_type=None, error_reason=None)
            with self.counter_lock:
                self.worker_outcomes[ticket.attempt] = row
            try:
                result = execute(token, ticket, request)
            except (Exception, SystemExit) as error:
                with self.counter_lock:
                    row.update(state='failed', **_diagnostic_error(error))
                raise
            with self.counter_lock:
                row.update(state='finished')
            return result

        def observe_decision(approval, session, reply, *, operation, **kwargs):
            if operation != 'answer':
                return decide(approval, session, reply, operation=operation, **kwargs)
            row = dict(request_id=reply.get('request_id') if isinstance(reply, dict) else None,
                       decision=None, launch=None, job=None)
            try:
                answer = decide(approval, session, reply, operation=operation, **kwargs)
            except Exception as error:
                row.update(_diagnostic_error(error))
                raise
            else:
                row.update({key: answer.get(key) for key in row})
                return answer
            finally:
                with self.counter_lock:
                    self.plan_answers.append(row)

        patch.setattr(self.channel.owner, 'execute_reserved', observe_execution)
        patch.setattr(desktop_route, 'decide_presented', observe_decision)

    def _install_launch_handoff_diagnostics(self, patch):
        from plugins.symphony_pm.transport.port import ServingPort
        from plugins.symphony_pm.typed_approval import TypedApproval

        owner = self.channel.owner
        grant = TypedApproval._grant
        submit = ServingPort.submit_intent

        def observe_grant(approval, item, route):
            result = grant(approval, item, route)
            if approval.owner is owner:
                request_id = item.request.request_id
                with self.counter_lock:
                    self.launch_handoffs[request_id] = dict(request_id=request_id,
                        grant_outcome='ISSUED' if result is not None else 'MISSING', port_outcome=None, uncertainty=[])
            return result

        def observe_submit(port, request, context):
            receipt = submit(port, request, context)
            if port.owner is owner:
                with self.counter_lock:
                    row = self.launch_handoffs.get(request.request_id)
                    if row is not None:
                        row.update(port_outcome=receipt.outcome,
                                   uncertainty=[_diagnostic_code(code) or 'OTHER' for code in receipt.uncertainty])
            return receipt

        patch.setattr(TypedApproval, '_grant', observe_grant)
        patch.setattr(ServingPort, 'submit_intent', observe_submit)

    def _worker_diagnostics(self):
        journal = self.channel.owner.journal
        with journal._transaction_lock, closing(sqlite3.connect(journal.path.as_uri() + '?mode=ro', uri=True)) as db:
            launches = db.execute('''SELECT operations.request_id, serving_launches.attempt,
                serving_launches.state, serving_launches.hold_reason
                FROM serving_launches JOIN operations USING (namespace, project, job)
                WHERE serving_launches.namespace=? AND operations.effect_key IS NULL
                ORDER BY operations.request_id''', (journal.namespace,)).fetchall()
        with self.counter_lock:
            outcomes = [dict(row) for row in self.worker_outcomes.values()]
            answers = [dict(row) for row in self.plan_answers]
            handoffs = [dict(row) for row in self.launch_handoffs.values()]
        return dict(state=self._worker('state')['state'], finished_event=self.finished.is_set(), launch_handoffs=handoffs,
                    outcomes=outcomes, plan_answers=answers,
                    launches=[dict(request_id=request, attempt_id=attempt, state=state, hold_reason=reason)
                              for request, attempt, state, reason in launches])

    def _register_personal(self, manifest, operator_key):
        # Only the harness writes this local fixture profile. It is outside both client sockets.
        home = Path(manifest['profile_id'])
        config = home / 'config.yaml'
        blob = json.loads(config.read_text(encoding='utf-8'))
        settings = blob['plugins']['entries']['symphony_pm']['settings']
        settings['desktop_forward_modes'][manifest['conversation_id']] = 'personal'
        settings['desktop_operator_registrations'] = {operator_key: dict(
            operator_id='trial-operator', generation=1, expires_at_ms=time.time_ns() // 1_000_000 + 600_000,
            rights=['submit', 'receipt', 'status', 'stop', 'stop_result', *self.result_rights],
        )}
        config.write_text(json.dumps(blob), encoding='utf-8')

    def _worker(self, action):
        if action == 'arm':
            if self.worker_armed.is_set() or self.worker_waiting.is_set():
                raise ValueError('worker fixture already armed')
            self.worker_released.clear()
            self.worker_armed.set()
        elif action == 'release':
            if not self.worker_waiting.is_set():
                raise ValueError('no held worker')
            self.worker_released.set()
        elif action != 'state':
            raise ValueError('unknown worker fixture operation')
        if self.worker_waiting.is_set():
            state = 'releasing' if self.worker_released.is_set() else 'held'
        else:
            state = 'armed' if self.worker_armed.is_set() else 'idle'
        return dict(state=state)

    def _stop_receipts(self):
        uri = Path(self.authority.db.db_path).resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as db:
            rows = db.execute('''SELECT json_extract(value,'$.result.command_id'),
                json_extract(value,'$.result.state'), json_extract(value,'$.result.attempt_id')
                FROM state_meta WHERE key GLOB 'gateway.desktop_operator.v1.*'
                  AND json_type(value,'$.result')='object' ORDER BY key''').fetchall()
        return [dict(command_id=command, state=state, attempt_id=attempt) for command, state, attempt in rows]

    async def control(self, cmd):
        operation = cmd['op']
        if operation == 'init':
            operator_key = cmd['operator_public_key']
            if _public_key(operator_key) == _public_key(cmd['public_key']):
                raise ValueError('TASK fixture requires separate ROUTE and OPERATOR keys')
            manifest = await super().control(cmd)
            self._register_personal(manifest, operator_key)
            return dict(manifest, forward_mode='personal')
        if operation == 'worker':
            return self._worker(cmd['action'])
        if operation == 'counts':
            counts = await super().control(cmd)
            rows = await asyncio.to_thread(self._stop_receipts)
            diagnostics = await asyncio.to_thread(self._worker_diagnostics)
            receipts = [row for row in rows if row['state'] != 'NOT_ACCEPTED']
            reconciliations = [row for row in rows if row['state'] == 'NOT_ACCEPTED']
            attempts = [dict(project=row['project'], job=row['job'], revision=row['revision'],
                             attempt_id=row['attempt']) for row in counts['attempts']]
            with self.counter_lock:
                stale_targets = self.stale_stop_targets
            return dict(counts, attempts=attempts, stop_commands=len(receipts), stop_receipts=receipts,
                        stop_reconciliations=reconciliations, stale_stop_targets=stale_targets,
                        worker_diagnostics=diagnostics)
        return await super().control(cmd)

    async def disconnect(self):
        self.worker_released.set()
        await super().disconnect()


if __name__ == '__main__':
    asyncio.run(run(OperatorPeer))
