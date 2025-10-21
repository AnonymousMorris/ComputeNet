import aiortc
import json


async def main():
    # 1. peer connection
    pc = aiortc.RTCPeerConnection()

    # peer connection listeners
    def onicecandidate(ice):
        print('Got ice candidate')
        print(json.dumps({
            'sdp': pc.localDescription.sdp,
            'type': pc.localDescription.type
        }))
        print(json.dumps(ice.toJSON()))
    pc.add_listener('icecandidate', onicecandidate)

    # 2. create data channel
    channel = pc.createDataChannel('test')

    # channel listeners
    def onopen():
        print('open')
        # 5. Send message
        channel.send('hello')
    def onmessage(message):
        print(message)
    def onclose():
        print('close')
        exit(0)
    channel.add_listener('open', onopen)
    channel.add_listener('message', onmessage)
    channel.add_listener('close', onclose)

    # 3. create offer
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    print(json.dumps({
        'sdp': pc.localDescription.sdp,
        'type': pc.localDescription.type
    }))

    # 4. create answer
    answer_json = input('Enter answer: ')
    answer_dict = json.loads(answer_json)

    answer = aiortc.RTCSessionDescription(answer_dict['sdp'], answer_dict['type'])
    await pc.setRemoteDescription(answer)

    await asyncio.Future()


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
