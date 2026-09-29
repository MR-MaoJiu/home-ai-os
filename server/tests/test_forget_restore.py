"""临时对象目录验证双日志重放和媒体清理，不生成实际备份。"""
import uuid
from sqlalchemy import select
from homeai.db import Record,scope,now
from homeai.media import MediaUpload,MediaChunk,MediaJob
from homeai.backup import replay_deletions,replay_memory_forgets,prune_restored_media
from homeai.auto_memory import remember_forget,forget_source_turns
from test_auto_memory import learn,actor,turn
from test_security_data import put,record


def test_restore_applies_forget_sources_and_prunes_deleted_media(system,alice,tmp_path):
    tid=turn(system,alice,'我喜欢听音乐')
    memory=learn(system,alice,'我喜欢听音乐',turn_id=tid)
    with system[2]() as db:
        scope(db,alice.user_id,'h1');remember_forget(system[0].state,db,actor(alice),db.get(Record,memory['record_id']));db.commit()
    media_record=put(alice,record(source_id='media',kind='photo.file',payload={'name':'隔离图片'}))
    upload_id=str(uuid.uuid4())
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        db.add(MediaUpload(id=upload_id,household_id='h1',owner_id=alice.user_id,client_id=str(uuid.uuid4()),request_hash='test-only',device_id=alice.device_id,kind='image',metadata_json='encrypted-test-only',size=1,sha256='0'*64,status='complete',expires_at=now()+300,record_id=media_record))
        db.add(MediaChunk(household_id='h1',owner_id=alice.user_id,upload_id=upload_id,position=0,size=1,sha256='0'*64))
        db.add(MediaJob(household_id='h1',owner_id=alice.user_id,record_id=media_record,upload_id=upload_id,device_id=alice.device_id,version=1))
        db.commit()
    journal=tmp_path/'deletions.jsonl'
    journal.write_text('\n'.join(system[0].state.vault.seal({'owner_id':alice.user_id,'record_id':identifier},'deletion-journal') for identifier in [memory['record_id'],media_record])+'\n')
    result=replay_deletions(system[2],system[0].state.vault,journal)
    assert result['removed_media_ids']==[upload_id]
    replay_memory_forgets(system[2],system[0].state.vault,system[0].state.settings.state_dir/'memory-forget.jsonl')
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert tid in forget_source_turns(db,actor(alice))
        assert db.get(Record,memory['record_id']).deleted
        assert db.get(MediaUpload,upload_id).status=='canceled'
        assert not db.scalar(select(MediaChunk).where(MediaChunk.upload_id==upload_id))
        assert db.scalar(select(MediaJob).where(MediaJob.upload_id==upload_id)).status=='canceled'
    restored=tmp_path/'isolated-restore'
    folder=restored/'media'/upload_id;folder.mkdir(parents=True);(folder/'0.enc').write_text('encrypted fixture')
    prune_restored_media(restored,result['removed_media_ids'])
    assert not folder.exists()
