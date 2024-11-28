import socket
from struct import pack
from ipaddress import ip_address
import argparse
import threading
import select
import os

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


def get_err(recv_msg:bytearray) -> None:
    '''에러 메시지를 처리하고 Exception 발생.

    클라이언트가 OP Code가 5(Error)를 수신했을 때 호출되어
    main() 함수의 except 블록이 처리할 수 있는 에러 메시지를 포함한 Exception을 발생시키고
    현재 송수신을 종료시킵니다.

    Args:
        recv_msg: 클라이언트가 수신한 bytearray 메시지.

    Raises:
        Exception: recv_msg에 포함된 에러 코드와 해당 에러 메시지를 담은 Exception.
    '''
    error_code = int.from_bytes(recv_msg[2:4], "big")
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

# def err_msg(err_no:int) -> bytes:
#     err_message = ERR[err_no].encode()
#     msg_format = f">hh{len(err_message)}sb"
#     return pack(msg_format, 5, err_no, err_message, 0)

class TftpSocket(socket.socket):
    '''TFTP 송수신에 필요한 기능을 부가한 UDP socket.
    
    Attributes:
        MAX_RETRY (int): 최대 재전송 허용 횟수. 
        BASETIME (float): timeout시 재전송 간격 기본 단위.
        retries (int): 마지막 송신 메시지의 재전송 횟수.
        timer (threading.Timer): 재전송 시간을 관리하는 타이머.
        lock (threading.Lock): timer 관리를 위한 lock.
        is_not_respond (threading.Event): 최대 재전송 횟수를 넘어 전송에 실패했음을 표시하는 flag.
        prev_msg (tuple(data, address)): 직전에 sendto()로 송신한 데이터와 소켓 주소.
    '''
    def __init__(self) -> None:
        super().__init__(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

        self.MAX_RETRY = 5
        self.BASETIME = 0.2
        self.retries = 0
        
        self.timer = None
        self.lock = threading.Lock()
        self.is_not_respond = threading.Event()

    def __start_timer(self) -> None:
        '''재전송 timeout 확인을 위한 타이머 생성.

        현재 재전송 횟수(retries)에 기반해 exponetial backoff에 따라 대기하는 타이머를 생성 및 시작합니다.
        - 2^(retries) * BASETIME
        '''
        with self.lock():
            if self.timer:
                # 이전 타이머가 취소 및 참조 해제되지 않은 경우 타이머 취소.
                self.timer.cancel()
            interval = 2 ** self.retries * self.BASETIME
            
            # 새 타이머에 __timeout()을 콜백으로 지정, 타이머를 변수에 할당하고 타이머 시작.
            self.timer = threading.Timer(interval, self.__timeout)
            self.timer.start()

    def __timeout(self) -> None:
        '''timer 만료시 재전송하거나 전송 실패 처리.

        현재 재전송 횟수(retries)가 최대 재전송 횟수(MAX_RETRY) 보다 작은 경우 메시지를 재전송합니다.
        그렇지 않은 경우 전송 실패 플래그(is_not_respond)를 활성화합니다.
        '''
        self.lock.acquire()
        if self.retries < self.MAX_RETRY:
            # 재전송 횟수 가산
            self.retries += 1
            print(f"retrying ... ({self.retries}/{self.MAX_RETRY})", end="\r")

            # sendto()가 prev_msg에 대한 lock을 요구하므로 호출 전 release
            prev_msg = self.prev_msg
            self.lock.release()

            # 직전에 송신한 메시지를 재전송
            self.sendto(prev_msg[0], prev_msg[1])
        else:
            # 최대 재전송 횟수에 도달한 경우 재전송하지 않고 전송 실패 처리
            self.is_not_respond.set()
            self.lock.release()

    def approve_recv(self) -> None:
        '''메시지 정상 수신 처리

        현재 전송한 메시지가 정상적으로 처리되었을 때 호출합니다.
        메시지가 timeout되지 않도록 timer를 취소합니다.
        '''
        with self.lock:
            # 재전송 대기 타이머가 존재하는 경우 취소하고 현재 재전송 횟수를 0으로 초기화
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self.retries = 0

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
        with self.lock:
            # 현재 전송할 메시지를 prev_msg에 저장
            self.prev_msg = (data, address)
        self.__start_timer()
        return super().sendto(data, address)
    
    def close(self) -> None:
        '''소켓 종료

        timer를 종료하고 소켓을 종료합니다.
        '''
        self.approve_recv()
        return super().close()

def get(sock:TftpSocket, socket_addr:tuple, file_name:str) -> bytearray:
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
    block_num = 0
    # TftpSocket의 전송 실패 플래그 참조
    is_not_respond = sock.is_not_respond
    

    # octet 모드로 file에 대한 RRQ 전송
    msg = rq_msg(1, file_name, "octet")
    sock.sendto(msg, socket_addr)

    while 1:
        # 현재 소켓이 수신한 메시지가 있는지 0.1초마다 확인
        readable, _, _ = select.select([sock], [], [], 0.1)

        # 전송 실패 처리되었는지 확인 후 실패했다면 TimeoutError 발생
        if is_not_respond.is_set():
            raise TimeoutError("Server not respond")
        # 수신한 메시지가 없는 경우 loop 건너뜀
        if sock not in readable:
            continue

        # 수신한 메시지와 소켓 주소를 recv_msg와 addr로 저장
        recv_msg, addr = sock.recvfrom(516)
        recv_msg = bytearray(recv_msg)

        # 수신 메시지가 OP Code 3의 정상 응답인 경우
        if recv_msg[0:2] == OP_CODE["DATA"]:
            current_block = int.from_bytes(recv_msg[2:4], "big")
            # 수신 메시지의 데이터 블록 번호가 이전 번호(최초 0)보다 클 경우
            if current_block > block_num:
                # 정상 수신 처리
                sock.approve_recv()

                # 현재 블록 번호의 ACK을 송신하고 블록 번호 교체
                sock.sendto(ack_msg(current_block), addr)
                block_num = current_block

                # 수신한 데이터 블록이 512바이트보다 작은 경우 마지막 블록으로 판단, 수신 종료
                recv_file += recv_msg[4:]
                if len(recv_msg[4:]) < 512:
                    sock.approve_recv()
                    break
            # 블록 번호가 잘못된 경우 메시지 무시
            else:
                continue

        # 수신 메시지가 OP Code 5의 에러 메시지인 경우
        elif recv_msg[0:2] == OP_CODE["ERROR"]:
            # 해당 예외를 발생시키고 수신 종료
            get_err(recv_msg)

    return recv_file


def put(sock:TftpSocket, socket_addr:tuple, file_name:str, send_file:bytearray) -> None:
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
    # TftpSocket의 전송 실패 플래그 참조
    is_not_respond = sock.is_not_respond

    # octet 모드로 file에 대한 WRQ 전송
    msg = rq_msg(2, file_name, "octet")
    sock.sendto(msg, socket_addr)

    # 현재 블록 번호가 송신할 마지막 블록 수보다 작거나 같을 때
    while block_num <= block_amount:
        # 현재 소켓이 수신한 메시지가 있는지 0.1초마다 확인
        readable, _, _ = select.select([sock], [], [], 0.1)

        # 전송 실패 처리되었는지 확인 후 실패했다면 TimeoutError 발생
        if is_not_respond.is_set():
            raise TimeoutError("Server not respond")
            
        # 수신한 메시지가 없는 경우 loop 건너뜀
        if sock not in readable:
            continue

        # 수신한 메시지와 소켓 주소를 recv_msg와 addr로 저장
        recv_msg, addr = sock.recvfrom(516)
        recv_msg = bytearray(recv_msg)

        # 수신 메시지가 OP Code 4의 정상 ACK인 경우
        if recv_msg[0:2] == OP_CODE["ACK"]:
            current_block = int.from_bytes(recv_msg[2:4], "big")

            # 수신 메시지의 ACK 번호가 이전 전송 번호와 같은 경우
            if current_block == block_num:
                # 정상 수신 처리
                sock.approve_recv()

                # 이전 전송 번호에 1을 가산하고 해당 블록 송신
                block_num = current_block + 1
                msg = data_msg(current_block, send_file[512*current_block:512*(current_block+1)])
                sock.sendto(msg, addr)
            # 블록 번호가 잘못된 경우 메시지 무시
            else:
                continue

        # 수신 메시지가 OP Code 5의 에러 메시지인 경우
        elif recv_msg[0:2] == OP_CODE["ERROR"]:
            # 해당 예외를 발생시키고 수신 종료
            get_err(recv_msg)
        
    sock.approve_recv()

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
            
            # TFTP get 수행 후 파일 저장
            recv_file = get(sock, socket_addr, file_name)
            with open(file_name, "wb") as file:
                file.write(recv_file)
            
            print(f"{file_name} has been saved")
        else:
            # 경로에 전송할 파일이 없는 경우 예외 발생
            if not os.path.isfile(file_name):
                raise Exception(f"There's no file \'{file_name}\'")
            
            # 파일을 불러온 후 TFTP put 수행
            with open(file_name, "rb") as file:
                send_file = bytearray(file.read())
            put(sock, socket_addr, send_file)
            print(f"{file_name} has been uploaded")
    
    # 예외 발생시 오류 메시지 출력
    except Exception as e:
        print(e)
    
    # 종료시 소켓과 타이머 종료
    finally:
        sock.close()
        if sock.timer:
            sock.approve_recv()

# 커맨드라인 인수 설정
parser = argparse.ArgumentParser()
parser.add_argument("address", help="tftp server IP address")
parser.add_argument("-p", "--port", default=69, type=int, help="tftp server service port")
parser.add_argument("mode", choices=["get", "put"], help="operation mode")
parser.add_argument("file_name")

args = parser.parse_args()
main(args)