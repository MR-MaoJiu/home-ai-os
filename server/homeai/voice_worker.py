"""持久通话处理器：转写、持续会话编排和朗读均在服务端执行。"""
import asyncio
import logging
from types import SimpleNamespace
from sqlalchemy import select, text
from .db import Device, Principal, Task, now, scope
from .security import Actor, own
from .voice_calls import VoiceCall, VoiceTurn, VoIPRegistration, TERMINAL
from .conversations import default_conversation, message, Message, text_result
from .contracts import TaskRequest
from .runtime import submit
from .result_access import check_dependencies

log = logging.getLogger('homeai.voice')


def result(app, actor, task):
    if not task.result: return {}
    return app.vault.open(task.result, actor.user_id + ':task-result:' + task.id)


def process_turn(app, actor, identifier):
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        turn = db.get(VoiceTurn, identifier)
        call = db.get(VoiceCall, turn.call_id)
        if call.status != 'ACTIVE': return
        db.refresh(turn, with_for_update=True)
        payload = app.vault.open(turn.payload, actor.user_id + ':voice-turn:' + turn.id)
        try:
            if turn.status == 'TRANSCRIBING':
                task = own(db, Task, payload['asr_task'], actor)
                if task.status == 'SUCCEEDED':
                    value = result(app, actor, task)
                    transcript = value.get('text')
                    if not isinstance(transcript, str) or not transcript.strip(): raise ValueError('没有识别到清晰语音，请重说')
                    payload['transcript'] = transcript[:10000]
                    conversation = default_conversation(app, db, actor)
                    payload['conversation_id'] = conversation.id
                    turn.status = 'SUBMITTING'
                elif task.status in {'FAILED','CANCELED','NEEDS_RECONCILIATION'}: raise ValueError('语音识别未完成，请检查本地语音服务')
            if turn.status == 'SUBMITTING':
                reply_to = None
                if call.source_task_id:
                    source = own(db, Task, call.source_task_id, actor)
                    source_body = app.vault.open(source.request, actor.user_id + ':task:' + source.id)
                    check_dependencies(db, actor, source_body)
                    if source_body.get('_conversation_id') == payload['conversation_id']: reply_to = source.id
                turn.payload = app.vault.seal(payload, actor.user_id + ':voice-turn:' + turn.id)
                db.commit()
                # 会话API拥有独立事务；固定client_key使崩溃后重试不会创建重复业务任务。
                response = message(payload['conversation_id'], Message(client_key='voice-call:' + turn.id,
                    content=payload['transcript'], reply_to_task_id=reply_to, timezone=app.vault.open(call.payload, actor.user_id + ':voice-call:' + call.id)['timezone']),
                    SimpleNamespace(app=SimpleNamespace(state=app)), actor)
                payload['chat_task'] = response['task_id']
                turn.status = 'THINKING'
            if turn.status == 'THINKING':
                task = own(db, Task, payload['chat_task'], actor)
                if task.status not in {'RECEIVED','EXECUTING','APPROVED'}:
                    body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
                    check_dependencies(db, actor, body)
                    answer = text_result(result(app, actor, task)) if task.status == 'SUCCEEDED' else (
                        '这次任务需要你在聊天中处理请求卡片，处理后任务会继续。' if task.status.startswith('WAITING') or task.status == 'AWAITING_APPROVAL'
                        else '这次任务没有完成，请在聊天中查看原因。')
                    payload['answer'] = answer or '任务已处理，请在聊天中查看结果。'
                    payload['waiting'] = task.status != 'SUCCEEDED'
                    spoken = payload['answer'][:2000]
                    if len(payload['answer']) > 2000: spoken += '。完整内容请在聊天中查看。'
                    speaker = app.vault.open(call.payload, actor.user_id + ':voice-call:' + call.id)['speaker']
                    payload['tts_tasks'] = []
                    for index in range(0, len(spoken), 250):
                        tts = submit(db, actor, TaskRequest(idempotency_key=f'call-tts:{turn.id}:{index}', capability='speech.synthesize@v1',
                            arguments={'text': spoken[index:index+250], 'speaker': speaker}, timeout_seconds=600, step_timeout_seconds=300), app.vault)
                        payload['tts_tasks'].append(tts.id)
                    turn.status = 'SYNTHESIZING'
            if turn.status == 'SYNTHESIZING':
                tasks = [own(db, Task, key, actor) for key in payload['tts_tasks']]
                if any(t.status in {'FAILED','CANCELED','NEEDS_RECONCILIATION'} for t in tasks): raise ValueError('语音合成未完成，文字答复已保存在聊天中')
                if all(t.status == 'SUCCEEDED' for t in tasks): turn.status = 'WAITING' if payload.get('waiting') else 'READY'
        except Exception as error:
            from fastapi import HTTPException
            # 用户没有提供业务数据；冲突仅延后提交，不产生重复消息。
            if isinstance(error, HTTPException) and error.status_code == 409 and turn.status == 'SUBMITTING':
                db.rollback(); return
            turn.status = 'FAILED'
            payload['error'] = str(error) if type(error) is ValueError else '本段语音处理未完成，请在聊天中查看任务状态'
        turn.payload = app.vault.seal(payload, actor.user_id + ':voice-turn:' + turn.id)
        db.commit()


async def cycle(app, apns):
    with app.db() as db: members = [(p.id,p.household_id,p.role) for p in db.scalars(select(Principal))]
    for user_id, household, role in members:
        with app.db() as db:
            scope(db,user_id,household)
            calls = list(db.scalars(select(VoiceCall).where(VoiceCall.owner_id==user_id, VoiceCall.status.not_in(TERMINAL))))
            for call in calls:
                device = db.get(Device,call.target_device_id)
                if not device or device.revoked or device.user_id!=user_id or call.expires_at<=now() or (call.status=='ACTIVE' and call.last_seen<now()-120):
                    call.status='MISSED' if call.status=='RINGING' else 'ENDED'
            db.commit()
            pending = [(c.id,c.target_device_id) for c in calls if c.status=='RINGING' and c.push_status=='PENDING']
            turns = [(c.id,c.target_device_id) for c in calls if c.status=='ACTIVE']
        for call_id,device_id in pending:
            with app.db() as db:
                scope(db,user_id,household)
                call=db.scalar(select(VoiceCall).where(VoiceCall.id==call_id).with_for_update())
                registration=db.scalar(select(VoIPRegistration).where(VoIPRegistration.device_id==device_id))
                if call.status!='RINGING' or call.push_status!='PENDING': continue
                if not registration: call.status='FAILED';db.commit();continue
                token=app.vault.open(registration.token,user_id+':voip:'+device_id)
                environment=registration.environment
                call.push_status='SENDING';db.commit()
            try:
                response=await apns.send_voip(token,environment,call_id)
                state='ACCEPTED' if response.status_code==200 else 'FAILED'
            except Exception: state='UNKNOWN'
            with app.db() as db:
                scope(db,user_id,household);call=db.get(VoiceCall,call_id)
                call.push_status=state
                if state=='FAILED' and call.status=='RINGING':call.status='FAILED'
                db.commit()
        for call_id,device_id in turns:
            actor=Actor(user_id,household,device_id,role)
            with app.db() as db:
                scope(db,user_id,household)
                identifiers=list(db.scalars(select(VoiceTurn.id).where(VoiceTurn.call_id==call_id,VoiceTurn.status.not_in(['READY','FAILED','WAITING']))))
            for identifier in identifiers: process_turn(app,actor,identifier)


async def main():
    from .api import create_app
    from .notifications import APNs
    app=create_app().state
    with app.db.kw['bind'].connect() as lock:
        if not lock.scalar(text('SELECT pg_try_advisory_lock(804238)')): raise SystemExit('已有语音通话Worker运行')
        apns=APNs(app.settings,app=app)
        try:
            while True:
                try: await cycle(app,apns)
                except Exception as error: log.error('通话循环失败：%s',type(error).__name__)
                await asyncio.sleep(1)
        finally: await apns.close()


if __name__=='__main__': asyncio.run(main())
