"""真实 UDP/DTLS 数据通道验收；不启动信令、中继或 HTTP 服务器。"""
import asyncio
import copy
import os
import sys
import time
import base64
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from homeai.direct_transport import DirectPeer, MAX_FRAME, validate_sdp, canonical, DOMAIN


async def main():
    left_key, right_key = [ec.generate_private_key(ec.SECP256R1()) for _ in range(2)]
    try:
        DirectPeer(left_key, right_key.public_key(), stun_urls=['turn:127.0.0.1:3478'])
        raise AssertionError('错误接受 TURN')
    except ValueError:
        pass
    left = DirectPeer(left_key, right_key.public_key())
    right = DirectPeer(right_key, left_key.public_key())
    try:
        offer = await left.offer()
        # 签名覆盖全部 SDP，包括临时 DTLS 公钥指纹和 ICE 候选。
        changed = copy.deepcopy(offer)
        changed['payload']['sdp'] += 'a=modified:true\r\n'
        try:
            await right.answer(changed)
            raise AssertionError('错误接受篡改信令')
        except InvalidSignature:
            pass
        expired = copy.deepcopy(offer)
        expired['payload']['expires'] = int(time.time()) - 1
        expired['signature'] = base64.b64encode(left_key.sign(DOMAIN + canonical(expired['payload']), ec.ECDSA(hashes.SHA256()))).decode()
        try:
            await right.answer(expired)
            raise AssertionError('错误接受过期信令')
        except ValueError:
            pass
        sdp = offer['payload']['sdp']
        assert ' typ host' in sdp
        try:
            validate_sdp(sdp.replace(' typ host', ' typ relay'))
            raise AssertionError('错误接受中继候选')
        except ValueError:
            pass
        answer = await right.answer(offer)
        await left.accept(answer)
        try:
            await left.accept(answer)
            raise AssertionError('错误接受重复协商')
        except ValueError:
            pass
        payload = os.urandom(256 * 1024)
        async def sender():
            for offset in range(0, len(payload), MAX_FRAME):
                await left.send(payload[offset:offset + MAX_FRAME])
        async def receiver():
            received = b''
            while len(received) < len(payload):
                received += await right.receive()
            assert received == payload
            await right.send(b'verified')
        await asyncio.gather(sender(), receiver())
        assert await left.receive() == b'verified'
        for peer in (left, right):
            assert peer.pc.connectionState == 'connected'
            assert peer.pc.sctp.transport.state == 'connected'
            assert all(c.type == 'host' for c in peer.pc.sctp.transport.transport.iceGatherer.getLocalCandidates())
        print('通过：真实 UDP/DTLS 双向传输 256 KiB；无 STUN/TURN/信令服务器；篡改、中继候选和重复响应被拒绝。')
        print('范围：本机两个端点，尚不证明跨 NAT、平台信令、iOS 或公网迁移完成。')
    finally:
        await asyncio.gather(left.close(), right.close())


asyncio.run(main())
