import os
import subprocess
import time
import threading
import zmq
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='static')

INPUT_URL  = os.environ.get("INPUT_URL",  "")
OUTPUT_URL = os.environ.get("OUTPUT_URL", "")
NGROK_TOKEN = os.environ.get("NGROK_TOKEN", "")

ZMQ_PORT = 5556

stream_status = {
    "running": False,
    "retries": 0,
    "current_text": "",
    "visible": False
}

overlay_config = {
    "text": "",
    "visible": False,
    "style": "scroll",
    "position_y": 90,
    "font_size": 48,
    "color": "white",
    "bg": True
}

# ── ZMQ sender ──
zmq_context = zmq.Context()

def send_zmq_command(command):
    try:
        sock = zmq_context.socket(zmq.REQ)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVTIMEO, 1000)
        sock.setsockopt(zmq.SNDTIMEO, 1000)
        sock.connect(f"tcp://127.0.0.1:{ZMQ_PORT}")
        sock.send_string(command)
        reply = sock.recv_string()
        sock.close()
        return True, reply
    except Exception as e:
        return False, str(e)

def update_overlay_live(config):
    text      = config.get("text", "")
    visible   = config.get("visible", False)
    color     = config.get("color", "white")
    font_size = config.get("font_size", 48)
    pos_y     = config.get("position_y", 90)
    style     = config.get("style", "scroll")
    bg        = config.get("bg", True)

    color_map = {
        "white":"white","yellow":"yellow","red":"red",
        "cyan":"cyan","lime":"lime","orange":"orange"
    }
    fc = color_map.get(color, "white")

    if not visible or not text.strip():
        send_zmq_command("Parsed_drawtext_0 reinit fontcolor=black@0")
        return

    safe_text = text.replace("'","").replace("\\","").replace(":","　").replace("\n"," ")

    if style == "scroll":
        x_expr = "W-mod(t*150\\,W+tw)"
    else:
        x_expr = "(W-tw)/2"

    y_expr = f"h*{pos_y}/100-th/2"
    bg_str = ":box=1:boxcolor=black@0.5:boxborderw=12" if bg else ""

    cmd = (
        f"Parsed_drawtext_0 reinit "
        f"text='{safe_text}'"
        f":fontsize={font_size}"
        f":fontcolor={fc}"
        f":x={x_expr}"
        f":y={y_expr}"
        f"{bg_str}"
    )

    ok, reply = send_zmq_command(cmd)
    print(f"ZMQ {'✅' if ok else '❌'}: {reply}")

# ── Flask API ──

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/overlay', methods=['GET'])
def get_overlay():
    return jsonify(overlay_config)

@app.route('/api/overlay', methods=['POST'])
def set_overlay():
    global overlay_config
    data = request.json
    overlay_config.update(data)
    update_overlay_live(overlay_config)
    stream_status["current_text"] = overlay_config.get("text", "")
    stream_status["visible"] = overlay_config.get("visible", False)
    return jsonify({"ok": True})

@app.route('/api/status')
def status():
    return jsonify(stream_status)

# ── FFmpeg ──

def build_ffmpeg_cmd():
    vf = (
        "fps=30,scale=1280:-2,"
        "drawtext=text=' '"
        ":fontsize=48"
        ":fontcolor=white@0"
        ":x=(W-tw)/2"
        ":y=h*0.9"
        ",zmq=bind_address=tcp\\://127.0.0.1\\:" + str(ZMQ_PORT)
    )
    return [
        'ffmpeg',
        '-loglevel', 'warning',
        '-err_detect', 'ignore_err',
        '-fflags', '+genpts+discardcorrupt',
        '-re',
        '-reconnect', '1',
        '-reconnect_at_eof', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '5',
        '-timeout', '10000000',
        '-i', INPUT_URL,
        '-vcodec', 'libx264',
        '-preset', 'ultrafast',
        '-tune', 'zerolatency',
        '-b:v', '2500k',
        '-maxrate', '2500k',
        '-bufsize', '5000k',
        '-pix_fmt', 'yuv420p',
        '-vf', vf,
        '-g', '60',
        '-keyint_min', '60',
        '-sc_threshold', '0',
        '-acodec', 'aac',
        '-b:a', '96k',
        '-ar', '44100',
        '-ac', '2',
        '-af', 'aresample=async=1000',
        '-f', 'flv',
        '-flvflags', 'no_duration_filesize',
        OUTPUT_URL
    ]

# ── Stream Thread ──

def start_stream():
    if not INPUT_URL or not OUTPUT_URL:
        print("❌ ERROR: تأكد من إضافة INPUT_URL و OUTPUT_URL في GitHub Secrets!")
        return

    while True:
        try:
            stream_status['running'] = True
            print(f"🚀 Starting stream... (attempt {stream_status['retries'] + 1})")

            cmd = build_ffmpeg_cmd()
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True
            )

            def reapply_overlay():
                time.sleep(4)
                if overlay_config.get("visible") and overlay_config.get("text"):
                    update_overlay_live(overlay_config)
            threading.Thread(target=reapply_overlay, daemon=True).start()

            for line in process.stdout:
                line = line.strip()
                if line and any(x in line for x in ['Error','error','fail','drop','Invalid']):
                    print(f"⚠️ {line}")

            process.wait()

        except Exception as e:
            print(f"❌ Exception: {e}")

        finally:
            stream_status['running'] = False
            stream_status['retries'] += 1
            print(f"🔄 Reconnecting in 3 seconds...")
            time.sleep(3)

# ── ngrok ──

def start_ngrok():
    if not NGROK_TOKEN:
        print("⚠️ NGROK_TOKEN غير موجود — لن يتم فتح النفق")
        return
    try:
        from pyngrok import ngrok, conf
        conf.get_default().auth_token = NGROK_TOKEN
        time.sleep(2)  # انتظر Flask يشتغل
        tunnel = ngrok.connect(7860)
        print("\n" + "="*55)
        print(f"🌐 رابط لوحة التحكم:")
        print(f"   {tunnel.public_url}")
        print("="*55 + "\n")
    except Exception as e:
        print(f"❌ ngrok error: {e}")

if __name__ == "__main__":
    os.makedirs('static', exist_ok=True)
    threading.Thread(target=start_stream, daemon=True).start()
    threading.Thread(target=start_ngrok, daemon=True).start()
    app.run(host="0.0.0.0", port=7860, threaded=True)
