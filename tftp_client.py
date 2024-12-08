import socket
from struct import pack
from ipaddress import ip_address
import argparse
import threading
import select
import os
import asyncio
from random import random

# TFTP 메시지 송수신에 사용하는 OP CODE 상수 선언
OP_CODE = {
    "RRQ": b"\00\01",
    "WRQ": b"\00\02",
    "DATA": b"\00\03",
    "ACK": b"\00\04",
    "ERROR": b"\00\05"
}

# TFTP 에러 코드별 상세 메시지 선언
ERR = {
    0: "Not defined, see error message (if any).",
    1: "File not found.",
    2: "Access violation.",
    3: "Disk full or allocation exceeded.",
    4: "Illegal TFTP operation.",
    5: "Unknown transfer ID.",
    6: "File already exists",
    7: "No such user."
}


def get_err(error_code:int) -> None:
    '''에러 메시지를 처리하고 Exception 발생.

    클라이언트가 OP Code 5(Error)를 수신했을 때 호출되어
    main() 함수의 except 블록이 처리할 수 있는 에러 메시지를 포함한 Exception을 발생시키고
    현재 송수신을 종료시킵니다.

    Args:
        recv_msg: 클라이언트가 수신한 bytearray 메시지.

    Raises:
        Exception: recv_msg에 포함된 에러 코드와 해당 에러 메시지를 담은 Exception.
    '''
    error_msg = ERR[error_code]
    raise Exception(f"Error code {error_code}: {error_msg}")

def rq_msg(op_code:int, filename:str, mode:str) -> bytes:
    '''TFTP RRQ/WRQ 메시지 생성.

    Args:
        op_code: 송수신 설정. get(RRQ): 1, put(WRQ): 2.
        file_name: 요청할 파일명.
        mode: 전송 모드 설정.

    Returns:
        TFTP RRQ/WRQ 메시지.
    '''

    filename = filename.encode()
    mode = mode.encode()
    msg_format = f">h{len(filename)}sb{len(mode)}sb"
    return pack(msg_format, op_code, filename, 0, mode, 0)

def data_msg(block_number:int, data:bytes) -> bytes:
    '''TFTP 송신 Data 메시지 생성.

    Args:
        block_number: 블록 일련번호.
        data: 블록 데이터.

    Returns:
        TFTP Data 메시지.
    '''
    msg_format = f">hh{len(data)}s"
    return pack(msg_format, 3, block_number, data)

def ack_msg(block_number:int) -> bytes:
    '''TFTP 수신 ACK 메시지 생성.

    Args:
        block_number: 수신한 블록 일련번호.

    Returns:
        TFTP ACK 메시지.
    '''
    msg_format = f">hh"
    return pack(msg_format, 4, block_number)


class TftpSocket(socket.socket):
    '''TFTP 송수신에 필요한 기능을 부가한 UDP socket.

    Attributes:
        MAX_RETRY (int): 최대 재전송 허용 횟수.
        BASETIME (float): timeout시 재전송 간격 기본 단위.
        retries (int): 마지막 송신 메시지의 재전송 횟수.
    '''
    def __init__(self) -> None:
        super().__init__(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.setblocking(False)

        self.MAX_RETRY = 5
        self.BASETIME = 0.2
        self.retries = 0

    async def __start_timer(self) -> None:
        '''수신 대기 타이머 설정

        지수 백오프에 기반한 재전송 시간만큼 대기합니다.
        (단위시간 * 2^(현재 재전송 횟수) + 무작위 밀리초 지연)
        '''
        interval = 2 ** self.retries * self.BASETIME + (random() / 100)
        await asyncio.sleep(interval)

    def __timeout(self) -> None:
        '''수신 대기 타이머 만료시 처리

        재전송 타이머에 callback으로 전달되어 타이머 만료시 호출됩니다.
        현재 재전송 횟수(retries)가 최대 재전송 횟수(MAX_RETRY) 보다 작은 경우 메시지를 재전송합니다.
        그렇지 않은 경우 socket을 닫고 TimeoutError를 발생시킵니다.

        Raises:
            TimeoutError: 최대 재전송 횟수에 도달한 경우 발생
        '''
        if self.retries < self.MAX_RETRY:
            # 재전송 횟수 가산
            self.retries += 1

            # 직전에 송신한 메시지를 재전송
            self.sendto(self.prev_msg[0], self.prev_msg[1])
            return
        else:
            # 최대 재전송 횟수 도달시 예외 발생
            raise TimeoutError("The server not respond.")

    def sendto(self, data:bytes, address:tuple) -> int:
        '''메시지 송신

        송신할 튜플(data, address)을 prev_msg에 저장하고 sendto로 송신합니다.
        __start_timer()를 호출해 타이머를 시작합니다.

        Args:
            data: 송신할 바이트 메시지.
            address: 송신할 소켓 주소.

        Returns:
            전송한 바이트 수.
        '''

        self.prev_msg = (data, address)
        return super().sendto(data, address)

    async def recv_msg(self, target_code:bytes, target_block:int, bufsize: int) -> tuple:
        '''메시지 수신 대기

        현재 순서에서 수신해야 할 메시지를 대기하고 목표한 메시지를 수신한 경우 반환합니다.

        Args:
            target_code: 수신해야 할 메시지의 OP Code.
            target_block: 수신해야 할 메시지의 block 번호.
            bufsize: 수신할 버퍼 크기

        Returns:
            수신 메시지, 수신 주소 튜플
        '''
        loop = asyncio.get_running_loop()

        # receive에서 task를 cancel할 때까지 반복적으로 메시지를 확인합니다.
        while 1:
            try:
                recv_msg, addr = await loop.run_in_executor(None, self.recvfrom, bufsize)
                
                # 수신한 메시지가 목표 메시지인 경우 해당 메시지를 반환합니다.
                if (recv_msg[0:2] == target_code):
                    if (int.from_bytes(recv_msg[2:4], "big") == target_block):
                        return (recv_msg, addr)
                # 수신한 메시지가 에러 메시지인 경우 get_err를 호출합니다.
                elif (recv_msg[0:2] == OP_CODE["ERROR"]):
                    get_err(int.from_bytes(recv_msg[2:4], "big"))
            except BlockingIOError:
                pass


    async def receive(self, target_code:bytes, target_block:int, bufsize: int) -> tuple:
        '''메시지 수신

        메시지를 수신 대기하고 정해진 시간 내에 목표 메시지를 수신하지 못한 경우 __timeout()을 호출합니다.
        시간 내에 목표 메시지를 수신한 경우 수신 데이터를 반환합니다.

        Args:
            target_code: 수신해야 할 메시지의 OP Code.
            target_block: 수신해야 할 메시지의 block 번호.
            bufsize: 수신할 버퍼 크기

        Returns:
            수신 메시지, 수신 주소 튜플
        '''

        # 수신과 타이머를 task로 설정
        receive = asyncio.create_task(self.recv_msg(target_code, target_block, bufsize))
        timer = asyncio.create_task(self.__start_timer())

        # 수신과 타이머 중 먼저 완료되는 것을 대기
        done, pending = await asyncio.wait(
            [receive, timer],
            return_when=asyncio.FIRST_COMPLETED
        )
        try:
            # 타이머가 완료된 시점에서 목표 메시지가 수신되지 않은 경우
            if timer in done:
                # 현재 수신 작업을 취소하고 timeout을 호출
                receive.cancel()
                self.__timeout()

                # 최대 재전송 횟수보다 현재 재전송 횟수가 작은 경우 receive를 재귀 호출
                return await self.receive(target_code, target_block, bufsize)

            # 타이머가 완료되기 전 목표 메시지가 수신된 경우
            elif receive in done:
                # 현재 타이머를 취소하고 현재 재전송 횟수를 0으로 초기화
                timer.cancel()
                self.retries = 0

                #수신 데이터 반환
                return receive.result()
        finally:
            # 만약 취소되지 않은 작업이 있는 경우 취소
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


async def get(sock:TftpSocket, socket_addr:tuple, file_name:str) -> bytearray:
    '''TFTP get 통신 수행

    Args:
        sock: TFTP 소켓.
        sock_addr: 목표 TFTP 서버의 주소(IP 주소, 포트 번호) 튜플.
        file_name: 목표 TFTP 서버의 파일명

    Returns:
        수신한 파일 바이너리

    Raises:
        TimeoutError: 시간 내 정상 응답이 수신되지 않을 경우 발생
        Exception: 서버로부터 에러 메시지를 수신할 경우 발생
    '''
    # 수신할 파일 바이너리
    recv_file = bytearray()
    # DATA 메시지의 현재 블록 번호
    block_num = 1

    # octet 모드로 file에 대한 RRQ 전송
    msg = rq_msg(1, file_name, "octet")
    sock.sendto(msg, socket_addr)

    while 1:
        # 수신한 메시지와 소켓 주소를 recv_msg와 addr로 저장
        recv_msg, addr = await sock.receive(OP_CODE["DATA"], block_num, 516)
        recv_msg = bytearray(recv_msg)

        sock.sendto(ack_msg(block_num), addr)
        block_num += 1

        # 수신한 데이터 블록이 512바이트보다 작은 경우 마지막 블록으로 판단, 수신 종료
        recv_file += recv_msg[4:]
        if len(recv_msg[4:]) < 512:
            break

    return recv_file

async def put(sock:TftpSocket, socket_addr:tuple, file_name:str, send_file:bytearray) -> None:
    '''TFTP put 통신 수행

    Args:
        sock: TFTP 소켓.
        sock_addr: 목표 TFTP 서버의 주소(IP 주소, 포트 번호) 튜플.
        file_name: 송신한 파일명
        send_file: 송신할 파일 바이너리

    Raises:
        TimeoutError: 시간 내 정상 응답이 수신되지 않을 경우 발생
        Exception: 서버로부터 에러 메시지를 수신할 경우 발생
    '''

    # 송신할 마지막 블록 번호
    block_amount = len(send_file)//512 + 1
    # 현재 블록 번호
    block_num = 0

    # octet 모드로 file에 대한 WRQ 전송
    msg = rq_msg(2, file_name, "octet")
    sock.sendto(msg, socket_addr)

    # 현재 블록 번호가 송신할 마지막 블록 번호보다 작은 동안
    while block_num < block_amount:
        recv_msg, addr = await sock.receive(OP_CODE["ACK"], block_num, 516)
        recv_msg = bytearray(recv_msg)
        
        # 다음 블록을 송신
        block_num += 1
        msg = data_msg(block_num, send_file[512*(block_num-1):512*(block_num)])
        sock.sendto(msg, addr)

def main(args:argparse.Namespace) -> None:
    '''TFTP 파일 송수신

    Args:
        args: 커맨드라인 인수
    '''

    # 소켓 주소와 포트 유효성 검증
    address = args.address
    try:
        ip_address(address)
    except ValueError:
        print("invalid address.")
        return
    port = args.port
    if 0 > port or 65535 < port:
        print("port number must be between 0 and 65535.")
        return

    mode = args.mode
    file_name = args.file_name

    #소켓 생성
    sock = TftpSocket()
    socket_addr = (address, port)

    try:
        if mode == "get":
            # 경로에 저장할 파일이 이미 존재하는 경우 덮어쓰기 확인
            if os.path.isfile(file_name):
                while 1:
                    answer = input(f"There's aleady exists file \'{file_name}\'. Do you replace it? (y/n) ")
                    if answer == "n":
                        return
                    elif answer == "y":
                        break

            # TFTP get 수행 후 수신한 바이너리 데이터를 파일로 저장
            recv_file = asyncio.run(get(sock, socket_addr, file_name))
            with open(file_name, "wb") as file:
                file.write(recv_file)
        else:
            # 경로에 전송할 파일이 없는 경우 예외 발생
            if not os.path.isfile(file_name):
                raise Exception(f"There's no file \'{file_name}\'")

            # 파일을 불러온 후 TFTP put 수행
            with open(file_name, "rb") as file:
                send_file = bytearray(file.read())
            asyncio.run(put(sock, socket_addr, file_name, send_file))

    # 예외 발생시 오류 메시지 출력
    except Exception as e:
        print(e)

    # 동작 완료시 소켓 종료
    finally:
        sock.close()

# 커맨드라인 인수 설정
parser = argparse.ArgumentParser()
parser.add_argument("address", help="tftp server IP address")
parser.add_argument("-p", "--port", default=69, type=int, help="tftp server service port")
parser.add_argument("mode", choices=["get", "put"], help="operation mode")
parser.add_argument("file_name")

args = parser.parse_args()
main(args)