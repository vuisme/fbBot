import os
import requests
import re
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from flask_redis import FlaskRedis
import time
import logging
import json

load_dotenv()

app = Flask(__name__)
app.config['REDIS_URL'] = os.getenv('REDIS_URL', 'redis://redis:6379/0')
redis_client = FlaskRedis(app)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
API_BASE_URL = os.getenv("API_BASE_URL")
API_BASE_URL2 = os.getenv("API_BASE_URL2")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

TIME_THRESHOLD = 300
SESSION_TTL = 900  # 15 minutes
HUMAN_CHAT_TTL = 900  # 15 minutes
TAOBAO_URL_PATTERN = re.compile(r'https://item.taobao.com/item.htm\?id=(\d+)')
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def send_message_facebook(recipient_id, message_text, quick_replies=None):
    headers = {
        'Content-Type': 'application/json'
    }
    message_data = {
        'text': message_text
    }
    if quick_replies:
        message_data['quick_replies'] = quick_replies
    else:
        message_data['quick_replies'] = [
            {
                "content_type": "text",
                "title": "Gặp CSKH",
                "payload": "HUMAN_CHAT"
            }
        ]
    
    data = {
        'recipient': {'id': recipient_id},
        'message': message_data
    }
    params = {
        'access_token': PAGE_ACCESS_TOKEN
    }
    response = requests.post(
        'https://graph.facebook.com/v20.0/me/messages',
        headers=headers,
        params=params,
        json=data
    )
    return response.json()

def send_message_telegram(chat_id, message_text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        'chat_id': chat_id,
        'text': message_text
    }
    response = requests.post(url, json=data)
    return response.json()

def process_taobao_link(taobao_id):
    response = requests.post(
        'http://dlcvn.vn:3000/get_tb_detailsq',
        headers={'Content-Type': 'application/json'},
        json={'id': taobao_id}
    )
    logger.info(response)
    if response.status_code == 200:
        return response.json()
    else:
        return None

def handle_taobao_message(sender_id, taobao_id):
    details_content = process_taobao_link(taobao_id)
    logger.info('detail content')
    logger.info(details_content)
    if details_content:
        headers = {
            'Content-Type': 'application/json'
        }
        
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(details_content)
                }
            ]
        }
        
        response = requests.post(
            f"{API_BASE_URL2}/api/chat/request",
            headers=headers,
            data=json.dumps(payload)
        )
        if response.status_code == 200:
            return response.json().get('result', {}).get('content', '')
        else:
            return None
    else:
        return None
        
@app.route('/webhook/facebook', methods=['GET', 'POST'])
def facebook_webhook():
    if request.method == 'GET':
        if request.args.get('hub.verify_token') == VERIFY_TOKEN:
            return request.args.get('hub.challenge')
        return 'Mã xác minh không khớp', 403

    if request.method == 'POST':
        data = request.json
        current_time = time.time()
        if 'entry' in data:
            for entry in data['entry']:
                if 'time' in entry and (current_time - entry['time'] / 1000) > TIME_THRESHOLD:
                    continue
                if 'messaging' in entry:
                    for messaging_event in entry['messaging']:
                        sender_id = messaging_event['sender']['id']
                        if messaging_event.get('message'):
                            # Check if in human chat mode
                            human_chat = redis_client.get(f"{sender_id}_human")
                            if human_chat:
                                logger.info(f"Message received during human chat: {messaging_event['message'].get('text')} from {sender_id}")
                                continue  # Skip auto-response

                            if 'quick_reply' in messaging_event['message']:
                                quick_reply_payload = messaging_event['message']['quick_reply']['payload']
                                if quick_reply_payload == 'RESET_SESSION':
                                    redis_client.delete(sender_id)
                                    send_message_facebook(sender_id, "Đã làm mới phiên chat, bạn có thể gửi báo giá cho món hàng mới")
                                    continue
                                elif quick_reply_payload == 'HUMAN_CHAT':
                                    redis_client.setex(f"{sender_id}_human", HUMAN_CHAT_TTL, "true")
                                    redis_client.setex(f"{sender_id}_last_message", HUMAN_CHAT_TTL, json.dumps({
                                        "timestamp": current_time,
                                        "sender": "admin"
                                    }))
                                    send_message_facebook(sender_id, "Bạn đã kết nối với nhân viên CSKH. Vui lòng chờ phản hồi từ người trực fanpage.")
                                    continue

                            message_text = messaging_event['message'].get('text')
                            if message_text:
                                logger.info(f"Received message: {message_text} from sender_id: {sender_id}")
                                session_data = redis_client.get(sender_id)
                                if session_data:
                                    chat_sessions = json.loads(session_data)
                                else:
                                    chat_sessions = []

                                logger.info(f"Old chat session: {chat_sessions}")
                                taobao_match = TAOBAO_URL_PATTERN.search(message_text)
                                if taobao_match:
                                    taobao_id = taobao_match.group(1)
                                    logger.info(taobao_id)
                                    assistant_message = handle_taobao_message(sender_id, taobao_id)
                                    if assistant_message:
                                        send_message_facebook(sender_id, assistant_message)
                                        logger.info("as mess")
                                        logger.info(assistant_message)
                                        chat_sessions.append({"role": "assistant", "content": assistant_message})
                                        redis_client.setex(sender_id, SESSION_TTL, json.dumps(chat_sessions))
                                    else:
                                        send_message_facebook(sender_id, "Xin lỗi, không thể xử lý liên kết Taobao.")
                                    continue
                                
                                chat_sessions.append({"role": "user", "content": message_text})

                                redis_client.setex(sender_id, SESSION_TTL, json.dumps(chat_sessions))

                                logger.info(f"Chat session for sender_id {sender_id}: {chat_sessions}")

                                # Update last message info
                                redis_client.setex(f"{sender_id}_last_message", HUMAN_CHAT_TTL, json.dumps({
                                    "timestamp": current_time,
                                    "sender": "user"
                                }))

                                response = requests.post(
                                    f"{API_BASE_URL}/api/chat/request",
                                    json={"messages": chat_sessions}
                                )
                                logger.info(f"API response: {response.status_code}")
                                logger.info(f"Full: {response}")
                                logger.info(f"API response text: {response.text}")
                                try:
                                    response_json = response.json()
                                    if response.status_code == 200:
                                        assistant_message = response_json.get('result', {}).get('content', '')
                                        chat_sessions.append({"role": "assistant", "content": assistant_message})
                                        
                                        # Update the Redis store with the new session data
                                        redis_client.setex(sender_id, SESSION_TTL, json.dumps(chat_sessions))

                                        quick_replies = [
                                            {
                                                "content_type": "text",
                                                "title": "Làm mới ngữ cảnh",
                                                "payload": "RESET_SESSION"
                                            },
                                            {
                                                "content_type": "text",
                                                "title": "Gặp CSKH",
                                                "payload": "HUMAN_CHAT"
                                            }
                                        ]
                                        send_message_facebook(sender_id, assistant_message, quick_replies)
                                    else:
                                        # Extract the error message from the detail array
                                        error_details = response_json.get('detail', [])
                                        if error_details:
                                            error_message = error_details[0].get('msg', 'Lỗi không xác định')
                                        else:
                                            error_message = 'Lỗi không xác định'

                                        send_message_facebook(sender_id, f"Xin lỗi, có lỗi xảy ra: {error_message}")

                                        # Log the full error response
                                        logger.error(f"Error detail: {response_json}")

                                        # Reset session context on error
                                        redis_client.delete(sender_id)
                                        send_message_facebook(sender_id, "Phiên chat đã được làm mới do lỗi.")
                                except requests.exceptions.JSONDecodeError:
                                    logger.error("JSON decode error", exc_info=True)
                                    send_message_facebook(sender_id, "Xin lỗi, có lỗi xảy ra: phản hồi không phải JSON hợp lệ.")

                                    # Reset session context on error
                                    redis_client.delete(sender_id)
                                    send_message_facebook(sender_id, "Phiên chat đã được làm mới do lỗi.")
                                except KeyError:
                                    logger.error("Unexpected response structure", exc_info=True)
                                    send_message_facebook(sender_id, "Xin lỗi, có lỗi xảy ra: cấu trúc phản hồi không mong đợi.")

                                    # Reset session context on error
                                    redis_client.delete(sender_id)
                                    send_message_facebook(sender_id, "Phiên chat đã được làm mới do lỗi.")

                        if messaging_event.get('postback'):
                            postback_payload = messaging_event['postback']['payload']
                            if postback_payload == 'RESET_SESSION':
                                redis_client.delete(sender_id)
                                send_message_facebook(sender_id, "Đã làm mới phiên chat, bạn có thể gửi báo giá cho món hàng mới")
                                continue
                            elif postback_payload == 'HUMAN_CHAT':
                                redis_client.setex(f"{sender_id}_human", HUMAN_CHAT_TTL, "true")
                                redis_client.setex(f"{sender_id}_last_message", HUMAN_CHAT_TTL, json.dumps({
                                    "timestamp": current_time,
                                    "sender": "admin"
                                }))
                                send_message_facebook(sender_id, "Bạn đã kết nối với con người. Vui lòng chờ phản hồi từ quản trị viên.")
                                continue

                        # Check for inactivity
                        human_chat = redis_client.get(f"{sender_id}_human")
                        if human_chat:
                            last_message_info = redis_client.get(f"{sender_id}_last_message")
                            if last_message_info:
                                last_message_info = json.loads(last_message_info)
                                last_message_time = last_message_info.get("timestamp")
                                last_message_sender = last_message_info.get("sender")
                                if (current_time - last_message_time) > HUMAN_CHAT_TTL and last_message_sender == "admin":
                                    redis_client.delete(f"{sender_id}_human")
                                    redis_client.delete(f"{sender_id}_last_message")
                                    send_message_facebook(sender_id, "Phiên chat với con người đã kết thúc do không có phản hồi. Chatbot sẽ tiếp tục hoạt động.")
        return 'SỰ KIỆN ĐÃ NHẬN', 200

    return 'Yêu cầu không hợp lệ', 400

@app.route('/webhook/telegram', methods=['POST'])
def telegram_webhook():
    data = request.json
    if 'message' in data:
        chat_id = data['message']['chat']['id']
        message_text = data['message'].get('text')
        current_time = time.time()
        if message_text:
            logger.info(f"Received message: {message_text} from chat_id: {chat_id}")

            session_data = redis_client.get(chat_id)
            if session_data:
                chat_sessions = json.loads(session_data)
            else:
                chat_sessions = []

            logger.info(f"Old chat session: {chat_sessions}")

            chat_sessions.append({"role": "user", "content": message_text})

            redis_client.setex(chat_id, SESSION_TTL, json.dumps(chat_sessions))

            logger.info(f"Chat session for chat_id {chat_id}: {chat_sessions}")

            # Update last message info
            redis_client.setex(f"{chat_id}_last_message", HUMAN_CHAT_TTL, json.dumps({
                "timestamp": current_time,
                "sender": "user"
            }))

            response = requests.post(
                f"{API_BASE_URL}/api/chat/request",
                json={"messages": chat_sessions}
            )
            logger.info(f"API response: {response.status_code}")
            logger.info(f"Full: {response}")
            logger.info(f"API response text: {response.text}")
            try:
                response_json = response.json()
                if response.status_code == 200:
                    assistant_message = response_json.get('result', {}).get('content', '')
                    chat_sessions.append({"role": "assistant", "content": assistant_message})
                    
                    # Update the Redis store with the new session data
                    redis_client.setex(chat_id, SESSION_TTL, json.dumps(chat_sessions))

                    send_message_telegram(chat_id, assistant_message)
                else:
                    # Extract the error message from the detail array
                    error_details = response_json.get('detail', [])
                    if error_details:
                        error_message = error_details[0].get('msg', 'Lỗi không xác định')
                    else:
                        error_message = 'Lỗi không xác định'
                    send_message_telegram(chat_id, f"Xin lỗi, có lỗi xảy ra: {error_message}")

                    # Log the full error response
                    logger.error(f"Error detail: {response_json}")

                    # Reset session context on error
                    redis_client.delete(chat_id)
                    send_message_telegram(chat_id, "Phiên chat đã được làm mới do lỗi.")
            except requests.exceptions.JSONDecodeError:
                logger.error("JSON decode error", exc_info=True)
                send_message_telegram(chat_id, "Xin lỗi, có lỗi xảy ra: phản hồi không phải JSON hợp lệ.")

                # Reset session context on error
                redis_client.delete(chat_id)
                send_message_telegram(chat_id, "Phiên chat đã được làm mới do lỗi.")
            except KeyError:
                logger.error("Unexpected response structure", exc_info=True)
                send_message_telegram(chat_id, "Xin lỗi, có lỗi xảy ra: cấu trúc phản hồi không mong đợi.")

                # Reset session context on error
                redis_client.delete(chat_id)
                send_message_telegram(chat_id, "Phiên chat đã được làm mới do lỗi.")

        # Check for inactivity
        human_chat = redis_client.get(f"{chat_id}_human")
        if human_chat:
            last_message_info = redis_client.get(f"{chat_id}_last_message")
            if last_message_info:
                last_message_info = json.loads(last_message_info)
                last_message_time = last_message_info.get("timestamp")
                last_message_sender = last_message_info.get("sender")
                if (current_time - last_message_time) > HUMAN_CHAT_TTL and last_message_sender == "admin":
                    redis_client.delete(f"{chat_id}_human")
                    redis_client.delete(f"{chat_id}_last_message")
                    send_message_telegram(chat_id, "Phiên chat với con người đã kết thúc do không có phản hồi. Chatbot sẽ tiếp tục hoạt động.")
    return 'OK', 200

@app.route('/reset_session', methods=['POST'])
def reset_session():
    data = request.json
    sender_id = data.get('sender_id')
    if sender_id:
        redis_client.delete(sender_id)
        return jsonify({"message": "Đã làm sạch phiên chat cũ"}), 200
    return jsonify({"message": "Invalid sender_id"}), 400

@app.route('/end_human_chat', methods=['POST'])
def end_human_chat():
    data = request.json
    sender_id = data.get('sender_id')
    if sender_id:
        redis_client.delete(f"{sender_id}_human")
        redis_client.delete(f"{sender_id}_last_message")
        return jsonify({"message": "Đã kết thúc chat với con người, chatbot sẽ tiếp tục hoạt động"}), 200
    return jsonify({"message": "Invalid sender_id"}), 400

@app.route('/healthz', methods=['GET'])
def healthz():
    return 'OK', 200
    
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
