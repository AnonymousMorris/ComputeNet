import aiortc
import json

async def main():
    pc = aiortc.RTCPeerConnection()

    def onicecandidate(ice):
        print("Got ice candidate")
        print(json.dumps(ice.toJSON()))

    def ondatachannel(chanel):
        print('got data channel')

        def onopen():
            print('open')
            chanel.send('hello')
        def onmessage(message):
            print(message)
        def onclose():
            print('close')

        chanel.add_listener('open', onopen)
        chanel.add_listener('message', onmessage)
        chanel.add_listener('close', onclose)

    pc.add_listener('icecandidate', onicecandidate)
    pc.add_listener('datachannel', ondatachannel)

    offer_json = input('Enter offer: ')
    offer_dict = json.loads(offer_json)
    offer = aiortc.RTCSessionDescription(offer_dict['sdp'], offer_dict['type'])
    await pc.setRemoteDescription(offer)

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    print()

    print(json.dumps({
        'sdp': pc.localDescription.sdp,
        'type': pc.localDescription.type
    }))

    await asyncio.Future()


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
