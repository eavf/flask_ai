from flask import Flask, render_template, request, url_for, flash, redirect, g, jsonify
from datetime import datetime
import os
import sqlite3
import yt_dlp
import speech_recognition as sr
from pydub import AudioSegment
import threading
import time
import wave

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
app.config['DEBUG'] = True

# Store transcription progress
transcription_progress = {}

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect('notes.db')
        g.db.row_factory = sqlite3.Row
    return g.db

def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    with app.open_resource('schema.sql') as f:
        db.executescript(f.read().decode('utf8'))

@app.cli.command('init-db')
def init_db_command():
    init_db()
    print('Initialized the database.')

def update_progress(task_id, status, progress=0, error=None):
    transcription_progress[task_id] = {
        'status': status,
        'progress': progress,
        'error': error
    }

def process_audio(url, task_id):
    try:
        update_progress(task_id, 'downloading', 0)
        
        def progress_hook(d):
            if d['status'] == 'downloading':
                if 'total_bytes' in d and d['total_bytes'] > 0:
                    progress = (d['downloaded_bytes'] / d['total_bytes']) * 100
                    update_progress(task_id, 'downloading', progress)
                elif 'downloaded_bytes' in d:
                    update_progress(task_id, 'downloading', -1)

        # Download audio
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'wav',
            }],
            'outtmpl': f'downloads/{task_id}.%(ext)s',
            'progress_hooks': [progress_hook],
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            duration = info.get('duration', 0)
            audio_file = f"downloads/{task_id}.wav"
        
        transcribe_audio(audio_file, task_id, f"Transcription: {info.get('title', 'Unknown')}", duration)
                
    except Exception as e:
        update_progress(task_id, 'error', 0, str(e))

def transcribe_audio(audio_file, task_id, title, duration=None):
    try:
        update_progress(task_id, 'transcribing', 0)
        
        # Get audio duration if not provided
        if duration is None:
            with wave.open(audio_file, 'rb') as wav_file:
                frames = wav_file.getnframes()
                rate = wav_file.getframerate()
                duration = frames / float(rate)
        
        # Transcribe audio
        recognizer = sr.Recognizer()
        with sr.AudioFile(audio_file) as source:
            audio = recognizer.record(source)
            total_steps = 10  # Number of steps for progress
            for step in range(total_steps):
                time.sleep(0.5)  # Simulate processing time
                update_progress(task_id, 'transcribing', (step + 1) * (100 / total_steps))
            
            transcription = recognizer.recognize_google(audio)
            
            # Save to database
            with app.app_context():
                db = get_db()
                db.execute(
                    'INSERT INTO notes (title, content, duration) VALUES (?, ?, ?)',
                    (title, transcription, int(duration))
                )
                db.commit()
            
            update_progress(task_id, 'completed', 100)
            
            # Clean up
            if os.path.exists(audio_file):
                os.remove(audio_file)
                
    except Exception as e:
        update_progress(task_id, 'error', 0, str(e))

@app.route('/')
def index():
    db = get_db()
    notes = db.execute('SELECT * FROM notes ORDER BY created DESC').fetchall()
    return render_template('index.html', notes=notes)

@app.route('/create', methods=('GET', 'POST'))
def create():
    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        error = None

        if not title:
            error = 'Title is required.'
        elif not content:
            error = 'Content is required.'

        if error is None:
            db = get_db()
            db.execute('INSERT INTO notes (title, content) VALUES (?, ?)',
                      (title, content))
            db.commit()
            flash('Note created successfully!', 'success')
            return redirect(url_for('index'))

        flash(error, 'error')

    return render_template('form.html')

@app.route('/speech_note', methods=('GET', 'POST'))
def speech_note():
    if request.method == 'POST':
        url = request.form['url']
        error = None

        if not url:
            error = 'URL is required.'

        if error is None:
            task_id = str(int(time.time()))
            thread = threading.Thread(target=process_audio, args=(url, task_id))
            thread.start()
            return jsonify({'task_id': task_id})

        flash(error, 'error')

    return render_template('speech_note.html')

@app.route('/record_note', methods=('GET', 'POST'))
def record_note():
    if request.method == 'POST':
        if 'audio' not in request.files:
            return jsonify({'error': 'No audio file provided'}), 400
        
        audio_file = request.files['audio']
        if audio_file.filename == '':
            return jsonify({'error': 'No selected file'}), 400
        
        task_id = str(int(time.time()))
        audio_path = f'downloads/{task_id}.wav'
        
        # Save the uploaded audio file
        audio_file.save(audio_path)
        
        # Start transcription in a background thread
        thread = threading.Thread(
            target=transcribe_audio, 
            args=(audio_path, task_id, "Recorded Note")
        )
        thread.start()
        
        return jsonify({'task_id': task_id})
        
    return render_template('record_note.html')

@app.route('/<int:id>/edit', methods=('GET', 'POST'))
def edit(id):
    note = get_db().execute('SELECT * FROM notes WHERE id = ?',
                           (id,)).fetchone()

    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        error = None

        if not title:
            error = 'Title is required.'
        elif not content:
            error = 'Content is required.'

        if error is None:
            db = get_db()
            db.execute('UPDATE notes SET title = ?, content = ? WHERE id = ?',
                      (title, content, id))
            db.commit()
            flash('Note updated successfully!', 'success')
            return redirect(url_for('index'))

        flash(error, 'error')

    return render_template('form.html', note=note)

@app.route('/<int:id>/delete', methods=('POST',))
def delete(id):
    db = get_db()
    db.execute('DELETE FROM notes WHERE id = ?', (id,))
    db.commit()
    flash('Note deleted successfully!', 'success')
    return redirect(url_for('index'))

@app.route('/progress/<task_id>')
def get_progress(task_id):
    return jsonify(transcription_progress.get(task_id, {'status': 'not_found'}))

app.teardown_appcontext(close_db)

if __name__ == '__main__':
    if not os.path.exists('notes.db'):
        with app.app_context():
            init_db()
    # Ensure downloads directory exists
    os.makedirs('downloads', exist_ok=True)
    app.run(debug=True)
