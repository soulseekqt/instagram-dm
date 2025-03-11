from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import os
import time
from datetime import datetime
import pytz
from instagrapi import Client
from dotenv import load_dotenv
import json
import threading
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)  # For session management

# Global client instance
instagram_clients = {}
active_polling_threads = {}

def get_client_for_user(username):
    """Get or create an Instagram client for the given user."""
    if username not in instagram_clients:
        instagram_clients[username] = Client()
    return instagram_clients[username]

def format_timestamp(timestamp, local_timezone):
    """Format the timestamp to show how long ago the message was sent."""
    # Convert the timestamp to the local timezone
    local_timestamp = timestamp.astimezone(local_timezone)
    now = datetime.now(local_timezone)  # Current time in the local timezone
    delta = now - local_timestamp
    if delta.days > 0:
        return f"{delta.days} day(s) ago"
    elif delta.seconds >= 3600:
        hours = delta.seconds // 3600
        return f"{hours} hour(s) ago"
    elif delta.seconds >= 60:
        minutes = delta.seconds // 60
        return f"{minutes} minute(s) ago"
    else:
        return f"{delta.seconds} second(s) ago"

def login_user(cl, username, password):
    """Attempts to login to Instagram."""
    session_file = f"session_{username}.json"

    # Check if session file exists
    if os.path.exists(session_file):
        try:
            cl.load_settings(session_file)
            cl.login(username, password)

            # Check if session is valid
            try:
                cl.get_timeline_feed()
                logger.info(f"Session loaded successfully for {username}")
                return True
            except Exception as e:
                logger.info(f"Loaded session is invalid: {e}")
        except Exception as e:
            logger.info(f"Error loading session: {e}")

    # Login with username and password
    try:
        logger.info(f"Logging in to Instagram as {username}...")
        cl.login(username, password)
        # Save session for future use
        cl.dump_settings(session_file)
        logger.info(f"New session created and saved successfully.")
        return True
    except Exception as e:
        logger.error(f"Failed to login: {e}")
        return False

def fetch_threads(cl, amount=10):
    """Fetch the most recent threads from the inbox."""
    try:
        threads = cl.direct_threads(amount=amount)
        return threads
    except Exception as e:
        logger.error(f"Failed to fetch threads: {e}")
        return []

def fetch_thread_messages(cl, thread_id, amount=20):
    """Fetch messages from a specific thread."""
    try:
        thread = cl.direct_thread(thread_id, amount=amount)
        return thread
    except Exception as e:
        logger.error(f"Failed to fetch messages for thread {thread_id}: {e}")
        return None

def send_message(cl, thread_id, text):
    """Send a message to a specific thread."""
    try:
        cl.direct_send(text, thread_ids=[thread_id])
        logger.info(f"Message sent to thread {thread_id}.")
        return True
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        return False

class MessagePollingThread(threading.Thread):
    """Thread for polling new messages in the background."""

    def __init__(self, username, thread_id):
        threading.Thread.__init__(self)
        self.username = username
        self.thread_id = thread_id
        self.stop_event = threading.Event()
        self.last_message_id = None
        self.daemon = True

    def run(self):
        """Poll for new messages."""
        polling_interval = 10  # Start with a 10-second interval
        cl = get_client_for_user(self.username)

        # Get initial message ID
        try:
            thread = fetch_thread_messages(cl, self.thread_id)
            if thread and thread.messages:
                self.last_message_id = thread.messages[0].id
        except Exception as e:
            logger.error(f"Error in initial message fetch: {e}")

        while not self.stop_event.is_set():
            try:
                thread = fetch_thread_messages(cl, self.thread_id)
                if thread and thread.messages and (self.last_message_id is None or thread.messages[0].id != self.last_message_id):
                    self.last_message_id = thread.messages[0].id
                    # Message updated, no need to refresh as client will poll
                    polling_interval = 10
            except Exception as e:
                logger.error(f"Error fetching messages: {e}")
                # Handle rate limits
                polling_interval = min(polling_interval * 2, 300)

            # Wait before polling again
            for _ in range(polling_interval):
                if self.stop_event.is_set():
                    break
                time.sleep(1)

    def stop(self):
        """Stop the polling thread."""
        self.stop_event.set()

@app.route('/')
def index():
    """Render the login page."""
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    """Handle login form submission."""
    username = request.form.get('username')
    password = request.form.get('password')

    if not username or not password:
        return render_template('login.html', error="Please provide both username and password")

    # Get the client for this user
    cl = get_client_for_user(username)

    # Try to log in
    if login_user(cl, username, password):
        # Store username in session
        session['username'] = username
        return redirect(url_for('threads'))
    else:
        return render_template('login.html', error="Login failed. Please check your credentials.")

@app.route('/logout')
def logout():
    """Handle user logout."""
    username = session.get('username')

    # Stop any active polling threads for this user
    for thread_key in list(active_polling_threads.keys()):
        if thread_key.startswith(f"{username}_"):
            active_polling_threads[thread_key].stop()
            del active_polling_threads[thread_key]

    # Logout from Instagram if client exists
    if username in instagram_clients:
        try:
            instagram_clients[username].logout()
        except:
            pass
        del instagram_clients[username]

    # Clear session
    session.pop('username', None)

    return redirect(url_for('index'))

@app.route('/threads')
def threads():
    """Display the user's message threads."""
    if 'username' not in session:
        return redirect(url_for('index'))

    username = session['username']
    cl = get_client_for_user(username)

    # Fetch threads
    threads_list = fetch_threads(cl)

    # Format threads for template
    formatted_threads = []
    for thread in threads_list:
        users = ", ".join([user.username for user in thread.users])
        formatted_threads.append({
            'id': thread.pk,
            'users': users,
            # # 'last_message': thread.last_permanent_item.text if hasattr(thread.last_permanent_item, 'text') and thread.last_permanent_item.text else "[Media or other content]",
            # 'timestamp': thread.last_permanent_item.timestamp
        })

    return render_template('threads.html', threads=formatted_threads)

@app.route('/chat/<thread_id>')
def chat(thread_id):
    """Display the chat interface for a specific thread."""
    if 'username' not in session:
        return redirect(url_for('index'))

    username = session['username']

    # Start polling thread if not already running
    thread_key = f"{username}_{thread_id}"
    if thread_key not in active_polling_threads:
        polling_thread = MessagePollingThread(username, thread_id)
        polling_thread.start()
        active_polling_threads[thread_key] = polling_thread

    return render_template('chat.html', thread_id=thread_id)

@app.route('/api/messages/<thread_id>')
def get_messages(thread_id):
    """API endpoint to get messages for a thread."""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    username = session['username']
    cl = get_client_for_user(username)
    current_user_id = cl.user_id

    # Get timezone from request or use default
    timezone_name = request.args.get('timezone', 'UTC')
    local_timezone = pytz.timezone(timezone_name)

    # Fetch thread
    thread = fetch_thread_messages(cl, thread_id)
    if not thread:
        return jsonify({'error': 'Failed to fetch messages'}), 500

    # Format messages
    formatted_messages = []
    for msg in thread.messages:
        # Get sender username
        if msg.user_id == current_user_id:
            sender_username = "You"
        else:
            sender_username = "User"
            for user in thread.users:
                if user.pk == msg.user_id:
                    sender_username = user.username
                    break

        # Format timestamp
        time_ago = format_timestamp(msg.timestamp, local_timezone)

        # Determine message type and content
        message_data = {
            'id': msg.id,
            'sender': sender_username,
            'timestamp': time_ago,
            'is_current_user': msg.user_id == current_user_id,
            'type': 'text'  # Default type
        }

        # Check for different types of media content
        if hasattr(msg, 'item_type'):
            if msg.item_type == 'text':
                message_data['type'] = 'text'
                message_data['text'] = msg.text or ""
            elif msg.item_type == 'media_share':
                message_data['type'] = 'media_share'
                if hasattr(msg, 'media_share') and hasattr(msg.media_share, 'thumbnail_url'):
                    message_data['media_url'] = msg.media_share.thumbnail_url
                    message_data['text'] = "[Shared Post]"
            elif msg.item_type == 'media':
                message_data['type'] = 'media'
                # For images and videos sent directly
                if hasattr(msg, 'visual_media') and hasattr(msg.visual_media, 'media'):
                    media = msg.visual_media.media
                    if hasattr(media, 'image_versions2'):
                        # Get the best quality image
                        candidates = media.image_versions2.candidates
                        if candidates:
                            message_data['media_url'] = candidates[0].url
                            message_data['text'] = "[Photo]"
                    elif hasattr(media, 'video_versions'):
                        # Get video URL
                        if media.video_versions:
                            message_data['media_url'] = media.video_versions[0].url
                            message_data['text'] = "[Video]"
                            message_data['video'] = True
            elif msg.item_type == 'voice_media':
                message_data['type'] = 'voice'
                message_data['text'] = "[Voice Message]"
                if hasattr(msg, 'voice_media') and hasattr(msg.voice_media, 'media') and hasattr(msg.voice_media.media, 'audio'):
                    message_data['media_url'] = msg.voice_media.media.audio.audio_src
            elif msg.item_type == 'story_share':
                message_data['type'] = 'story'
                message_data['text'] = "[Shared Story]"
            elif msg.item_type == 'reel_share':
                message_data['type'] = 'reel'
                message_data['text'] = "[Shared Reel]"
            elif msg.item_type == 'clip':
                message_data['type'] = 'clip'
                message_data['text'] = "[Clip]"
                if hasattr(msg, 'clip') and hasattr(msg.clip, 'media') and hasattr(msg.clip.media, 'video_versions'):
                    message_data['media_url'] = msg.clip.media.video_versions[0].url
                    message_data['video'] = True
            else:
                # For other types
                message_data['type'] = 'other'
                message_data['text'] = f"[{msg.item_type}]"
        else:
            # If no item_type attribute, default to text
            message_data['text'] = msg.text or "[Media or other content]"

        formatted_messages.append(message_data)

    # Thread info
    thread_info = {
        'id': thread.pk,
        'users': [{'username': user.username, 'pk': user.pk} for user in thread.users]
    }

    return jsonify({
        'thread': thread_info,
        'messages': formatted_messages
    })

@app.route('/api/send/<thread_id>', methods=['POST'])
def send_message_api(thread_id):
    """API endpoint to send a message."""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    username = session['username']
    cl = get_client_for_user(username)

    # Get message text from request
    data = request.get_json()
    message_text = data.get('message', '').strip()

    if not message_text:
        return jsonify({'error': 'Message cannot be empty'}), 400

    # Send the message
    if send_message(cl, thread_id, message_text):
        return jsonify({'success': True})
    else:
        return jsonify({'error': 'Failed to send message'}), 500

# Create the HTML templates directory
os.makedirs('templates', exist_ok=True)

# Create HTML templates
with open('templates/login.html', 'w') as f:
    f.write('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Instagram DM - Login</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.0/css/bootstrap.min.css">
    <style>
        body {
            background-color: #fafafa;
            padding-top: 60px;
        }
        .login-container {
            max-width: 350px;
            padding: 20px;
            background-color: white;
            border-radius: 5px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .logo {
            text-align: center;
            margin-bottom: 20px;
            font-family: 'Billabong', cursive;
            font-size: 35px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="row justify-content-center">
            <div class="login-container">
                <div class="logo">Instagram DM</div>
                {% if error %}
                <div class="alert alert-danger">{{ error }}</div>
                {% endif %}
                <form method="POST" action="/login">
                    <div class="mb-3">
                        <input type="text" class="form-control" name="username" placeholder="Username" required>
                    </div>
                    <div class="mb-3">
                        <input type="password" class="form-control" name="password" placeholder="Password" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">Log In</button>
                </form>
                <div class="mt-3 text-center text-muted small">
                    <p>This web app is for personal use only. It uses your credentials securely.</p>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
''')

with open('templates/threads.html', 'w') as f:
    f.write('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Instagram DM - Conversations</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.0/css/bootstrap.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        body {
            background-color: #fafafa;
        }
        .thread-list {
            background-color: white;
            border-radius: 5px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .thread-item {
            border-bottom: 1px solid #efefef;
            padding: 15px;
            cursor: pointer;
        }
        .thread-item:hover {
            background-color: #f9f9f9;
        }
        .thread-item:last-child {
            border-bottom: none;
        }
        .thread-title {
            font-weight: bold;
        }
        .thread-preview {
            color: #8e8e8e;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .thread-time {
            font-size: 0.8rem;
            color: #8e8e8e;
        }
        .navbar {
            background-color: white;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-light fixed-top">
        <div class="container">
            <a class="navbar-brand" href="/threads">Instagram DM</a>
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
                <span class="navbar-toggler-icon"></span>
            </button>
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav ms-auto">
                    <li class="nav-item">
                        <a class="nav-link" href="/logout"><i class="fas fa-sign-out-alt"></i> Logout</a>
                    </li>
                </ul>
            </div>
        </div>
    </nav>

    <div class="container" style="padding-top: 70px;">
        <h4 class="mb-3">Direct Messages</h4>

        <div class="thread-list">
            {% if threads %}
                {% for thread in threads %}
                <div class="thread-item" onclick="window.location.href='/chat/{{ thread.id }}'">
                    <div class="d-flex justify-content-between">
                        <div class="thread-title">{{ thread.users }}</div>
                        <div class="thread-time"></div>
                    </div>
                    <div class="thread-preview">{{ thread.last_message }}</div>
                </div>
                {% endfor %}
            {% else %}
                <div class="text-center p-4 text-muted">
                    <p>No conversations found</p>
                </div>
            {% endif %}
        </div>
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script>
</body>
</html>
''')

with open('templates/chat.html', 'w') as f:
    f.write('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Instagram DM - Chat</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.0/css/bootstrap.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        body {
            background-color: #fafafa;
            height: 100vh;
            display: flex;
            flex-direction: column;
        }
        .navbar {
            background-color: white;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .chat-container {
            flex: 1;
            display: flex;
            flex-direction: column;
            padding: 70px 0 0;
        }
        .message-list {
            flex: 1;
            overflow-y: auto;
            padding: 15px;
            display: flex;
            flex-direction: column;
        }
        .message {
            max-width: 75%;
            margin-bottom: 10px;
            padding: 10px 15px;
            border-radius: 15px;
            position: relative;
            word-wrap: break-word;
        }
        .message.outgoing {
            background-color: #007AFF; /* iMessage blue */
            color: white;
            align-self: flex-end;
            margin-left: auto;
            border-bottom-right-radius: 5px; /* Sharp edge on bottom right */
        }
        .message.incoming {
            background-color: #E5E5EA; /* Light grey */
            color: black;
            align-self: flex-start;
            margin-right: auto;
            border-bottom-left-radius: 5px; /* Sharp edge on bottom left */
        }
        .message-sender {
            font-weight: bold;
            font-size: 0.8rem;
            margin-bottom: 2px;
        }
        .message-time {
            font-size: 0.7rem;
            color: #8e8e8e;
            margin-top: 2px;
            text-align: right;
        }
        .outgoing .message-time {
            color: rgba(255, 255, 255, 0.8);
        }
        .message-media {
            max-width: 100%;
            max-height: 300px;
            border-radius: 5px;
            margin-top: 5px;
            cursor: pointer;
        }
        .message-video {
            max-width: 100%;
            max-height: 300px;
            border-radius: 5px;
            margin-top: 5px;
        }
        .media-container {
            position: relative;
        }
        .media-play-button {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            color: white;
            background-color: rgba(0, 0, 0, 0.5);
            border-radius: 50%;
            padding: 10px;
            font-size: 20px;
        }
        .message-input-container {
            background-color: white;
            border-top: 1px solid #dbdbdb;
            padding: 10px;
        }
        .message-input {
            border-radius: 20px;
            padding: 10px 50px 10px 15px;
            resize: none;
            width: 100%;
            border: 1px solid #dbdbdb;
            outline: none;
            line-height: 1.5;
        }
        .send-button {
            position: absolute;
            right: 25px;
            bottom: 18px; /* Adjusted to align with input */
            background: none;
            border: none;
            color: #007AFF;
            font-size: 18px;
            cursor: pointer;
            padding: 0;
            margin: 0;
        }
        .loading-spinner {
            display: none;
            text-align: center;
            padding: 20px;
        }
        /* Media Preview Modal */
        .media-modal {
            display: none;
            position: fixed;
            z-index: 1050;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            overflow: auto;
            background-color: rgba(0,0,0,0.9);
        }
        .media-modal-content {
            margin: auto;
            display: block;
            max-width: 90%;
            max-height: 90vh;
        }
        .close-modal {
            position: absolute;
            top: 15px;
            right: 35px;
            color: #f1f1f1;
            font-size: 40px;
            font-weight: bold;
            cursor: pointer;
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-light fixed-top">
        <div class="container">
            <a class="navbar-brand" href="/threads"><i class="fas fa-arrow-left"></i> Back</a>
            <span id="chat-title" class="navbar-text">Loading...</span>
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
                <span class="navbar-toggler-icon"></span>
            </button>
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav ms-auto">
                    <li class="nav-item">
                        <a class="nav-link" href="#" id="refresh-btn"><i class="fas fa-sync-alt"></i> Refresh</a>
                    </li>
                </ul>
            </div>
        </div>
    </nav>

    <div class="chat-container">
        <div class="message-list" id="messageList">
            <div class="loading-spinner" id="loadingSpinner">
                <div class="spinner-border text-primary" role="status">
                    <span class="visually-hidden">Loading...</span>
                </div>
            </div>
        </div>

        <div class="message-input-container">
            <div class="container position-relative">
                <form id="messageForm">
                    <textarea class="form-control message-input" id="messageInput" placeholder="Message..." rows="1"></textarea>
                    <button type="submit" class="send-button"><i class="fas fa-paper-plane"></i></button>
                </form>
            </div>
        </div>
    </div>

    <!-- Media Preview Modal -->
    <div id="mediaModal" class="media-modal">
        <span class="close-modal">&times;</span>
        <img class="media-modal-content" id="mediaPreview">
        <video class="media-modal-content" id="videoPreview" controls style="display:none;"></video>
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script>
    <script>
        // Get the thread ID from the URL
        const threadId = '{{ thread_id }}';
        let lastMessageId = null;

        // Get user's timezone
        const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

        // Function to load messages
        function loadMessages() {
            document.getElementById('loadingSpinner').style.display = 'block';

            fetch(`/api/messages/${threadId}?timezone=${encodeURIComponent(timezone)}`)
                .then(response => response.json())
                .then(data => {
                    if (data.error) {
                        console.error(data.error);
                        return;
                    }

                    // Update the chat title
                    const users = data.thread.users.map(user => user.username).join(', ');
                    document.getElementById('chat-title').textContent = users;

                    // Clear messages if first load or if we have a new message
                    const messageList = document.getElementById('messageList');
                    if (!lastMessageId || (data.messages.length > 0 && data.messages[0].id !== lastMessageId)) {
                        // Keep the spinner element
                        const spinner = document.getElementById('loadingSpinner');
                        messageList.innerHTML = '';
                        messageList.appendChild(spinner);

                        // Update lastMessageId if we have messages
                        if (data.messages.length > 0) {
                            lastMessageId = data.messages[0].id;
                        }

                        // Reverse the messages array to show newest at the bottom
                        const sortedMessages = [...data.messages].reverse();

                        // Add messages
                        sortedMessages.forEach(message => {
                            const messageDiv = document.createElement('div');
                            messageDiv.className = `message ${message.is_current_user ? 'outgoing' : 'incoming'}`;
                            messageDiv.dataset.messageId = message.id;

                            if (!message.is_current_user) {
                                const senderDiv = document.createElement('div');
                                senderDiv.className = 'message-sender';
                                senderDiv.textContent = message.sender;
                                messageDiv.appendChild(senderDiv);
                            }

                            const textDiv = document.createElement('div');
                            textDiv.className = 'message-text';
                            textDiv.textContent = message.text;
                            messageDiv.appendChild(textDiv);

                            // Handle media content
                            if (message.type !== 'text' && message.media_url) {
                                if (message.video) {
                                    // For video content
                                    const videoElement = document.createElement('video');
                                    videoElement.className = 'message-video';
                                    videoElement.src = message.media_url;
                                    videoElement.controls = true;
                                    videoElement.style.maxWidth = '100%';
                                    videoElement.style.maxHeight = '300px';
                                    videoElement.style.borderRadius = '5px';
                                    videoElement.style.marginTop = '5px';
                                    messageDiv.appendChild(videoElement);
                                } else {
                                    // For image content
                                    const mediaImg = document.createElement('img');
                                    mediaImg.className = 'message-media';
                                    mediaImg.src = message.media_url;
                                    mediaImg.alt = 'Media';
                                    mediaImg.style.maxWidth = '100%';
                                    mediaImg.style.maxHeight = '300px';
                                    mediaImg.style.borderRadius = '5px';
                                    mediaImg.style.marginTop = '5px';
                                    mediaImg.onclick = function() { openMediaModal(message.media_url, false); };
                                    messageDiv.appendChild(mediaImg);
                                }
                            }

                            const timeDiv = document.createElement('div');
                            timeDiv.className = 'message-time';
                            timeDiv.textContent = message.timestamp;
                            messageDiv.appendChild(timeDiv);

                            messageList.appendChild(messageDiv);
                        });

                        // Scroll to the bottom
                        messageList.scrollTop = messageList.scrollHeight;
                    }
                })
                .catch(error => {
                    console.error('Error loading messages:', error);
                })
                .finally(() => {
                    document.getElementById('loadingSpinner').style.display = 'none';
                });
        }

        // Function to open media preview modal
        function openMediaModal(src, isVideo) {
            const modal = document.getElementById('mediaModal');
            const imagePreview = document.getElementById('mediaPreview');
            const videoPreview = document.getElementById('videoPreview');

            if (isVideo) {
                imagePreview.style.display = 'none';
                videoPreview.style.display = 'block';
                videoPreview.src = src;
                videoPreview.play();
            } else {
                imagePreview.style.display = 'block';
                videoPreview.style.display = 'none';
                imagePreview.src = src;
            }

            modal.style.display = 'flex';
        }

        // Function to close the modal
        function closeMediaModal() {
            const modal = document.getElementById('mediaModal');
            const videoPreview = document.getElementById('videoPreview');

            modal.style.display = 'none';
            videoPreview.pause();
        }

        // Function to send a message
        function sendMessage(messageText) {
            fetch(`/api/send/${threadId}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    message: messageText
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.error) {
                    console.error(data.error);
                    return;
                }

                // Clear the input
                document.getElementById('messageInput').value = '';

                // Load the messages to show the sent message
                setTimeout(loadMessages, 1000);
            })
            .catch(error => {
                console.error('Error sending message:', error);
            });
        }

        // Initialize the chat
        document.addEventListener('DOMContentLoaded', function() {
            // Load messages initially
            loadMessages();

            // Set up polling for new messages
            setInterval(loadMessages, 10000);

            // Set up the form submission
            document.getElementById('messageForm').addEventListener('submit', function(e) {
                e.preventDefault();

                const messageInput = document.getElementById('messageInput');
                const messageText = messageInput.value.trim();

                if (messageText) {
                    sendMessage(messageText);
                }
            });

            // Set up the refresh button
            document.getElementById('refresh-btn').addEventListener('click', function(e) {
                e.preventDefault();
                loadMessages();
            });

            // Make textarea expand with content
            const messageInput = document.getElementById('messageInput');
            messageInput.addEventListener('input', function() {
                this.style.height = 'auto';
                this.style.height = (this.scrollHeight) + 'px';
            });

            // Set up modal close event
            document.querySelector('.close-modal').addEventListener('click', closeMediaModal);

            // Close modal when clicking outside of content
            document.getElementById('mediaModal').addEventListener('click', function(e) {
                if (e.target === this) {
                    closeMediaModal();
                }
            });
        });
    </script>
</body>
</html>
''')

# Main entry point
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8000, debug=True)
