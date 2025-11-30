import os
from flask import Flask, request, jsonify, Response
import requests
import logging
import time

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

NAVIDROME_URL = os.getenv("NAVIDROME_URL", None)
DEEZER_ARL = os.getenv("DEEZER_ARL", None)
DEEMIX_URL = os.getenv("DEEMIX_URL", None)

DEEZER_API_URL = "https://api.deezer.com"

# Cache to improve performance
search_cache = {}
CACHE_TIMEOUT = 300  # 5 minutes

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    """Catch all routes and proxy to Navidrome or handle Deezer requests"""
    start_time = time.time()
    
    try:
        # Handle streaming requests
        if path.startswith('rest/stream') or path.startswith('rest/download'):
            track_id = request.args.get('id')
            if track_id and track_id.startswith('deezer_'):
                logger.info(f"Streaming Deezer track: {track_id}")
                return stream_deezer_track(track_id)
        
        # Handle search requests
        if path.startswith('rest/search'):
            query = request.args.get('query')
            if query:
                # Check cache first
                cache_key = f"search_{query}"
                if cache_key in search_cache:
                    cached_data, cache_time = search_cache[cache_key]
                    if time.time() - cache_time < CACHE_TIMEOUT:
                        logger.info(f"Using cached search results for: {query}")
                        return jsonify(cached_data)
                
                return handle_search_request(path)
        
        # Handle cover art
        if path.startswith('rest/getCoverArt'):
            cover_id = request.args.get('id', '')
            if cover_id and (cover_id.startswith('deezer_') or cover_id.startswith('al-') or cover_id.isdigit()):
                if cover_id.startswith('deezer_'):
                    album_id = cover_id.replace('deezer_', '')
                elif cover_id.startswith('al-'):
                    album_id = cover_id.replace('al-', '')
                else:
                    album_id = cover_id
                return get_deezer_cover_art(album_id)
        
        # Handle "love" (star) for Deezer tracks - trigger download
        if path.startswith('rest/setRating') or path.startswith('rest/star') or path.startswith('rest/unstar'):
            track_id = request.args.get('id')
            if track_id and track_id.startswith('deezer_'):
                return trigger_deezer_download(track_id)
        
        # Proxy everything else directly to Navidrome
        return proxy_to_navidrome(path)
        
    except Exception as e:
        logger.error(f"Error handling request: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        logger.debug(f"Request {path} took {time.time() - start_time:.2f}s")

def proxy_to_navidrome(path):
    """Fast proxy to Navidrome without processing"""
    url = f"{NAVIDROME_URL}/{path}"
    
    resp = requests.request(
        method=request.method,
        url=url,
        params=request.args,
        headers={k: v for k, v in request.headers if k.lower() != 'host'},
        data=request.get_data(),
        stream=True,
        timeout=10
    )
    
    headers = [(name, value) for name, value in resp.raw.headers.items()
              if name.lower() not in ['content-encoding', 'content-length', 'transfer-encoding', 'connection']]
    
    return Response(resp.iter_content(chunk_size=8192), resp.status_code, headers)

def handle_search_request(path):
    """Handle search requests and merge Deezer results - RELIABLE VERSION"""
    try:
        # Get Navidrome results first
        navidrome_response = proxy_to_navidrome(path)
        
        if navidrome_response.status_code != 200:
            return navidrome_response
        
        query = request.args.get('query')
        if not query:
            return navidrome_response
        
        # Get Deezer results (synchronously - more reliable)
        deezer_results = get_deezer_results(query)
        
        if deezer_results.get('song'):
            # Parse Navidrome JSON and merge
            navidrome_data = navidrome_response.get_json()
            merged_data = merge_search_results(navidrome_data, deezer_results)
            
            # Cache the results
            cache_key = f"search_{query}"
            search_cache[cache_key] = (merged_data, time.time())
            
            return jsonify(merged_data)
        else:
            return navidrome_response
            
    except Exception as e:
        logger.error(f"Search error: {e}")
        return proxy_to_navidrome(path)

def get_deezer_results(query):
    """Search Deezer for tracks"""
    try:
        logger.info(f"Searching Deezer for: {query}")
        response = requests.get(f"{DEEZER_API_URL}/search", params={'q': query, 'limit': 250}, timeout=8)
        
        if response.status_code != 200:
            logger.warning(f"Deezer API returned {response.status_code}")
            return {"song": []}
            
        deezer_data = response.json()
        logger.info(f"Found {len(deezer_data.get('data', []))} Deezer results")
        return format_deezer_results(deezer_data)
        
    except Exception as e:
        logger.error(f"Deezer search error: {e}")
        return {"song": []}

def format_deezer_results(deezer_data):
    """Format Deezer results to match Subsonic API EXACTLY"""
    songs = []
    
    for track in deezer_data.get('data', []):
        # Use the exact field names that Navidrome uses
        song = {
            'id': f"deezer_{track['id']}",
            'parent': f"deezer_album_{track['album']['id']}",
            'isDir': False,
            'title': track['title_short'] if 'title_short' in track else track['title'],
            'album': track['album']['title'],
            'artist': "{} - deezer preview".format(track['artist']['name']),
            'track': track.get('track_position', 1),
            'year': track['album']['release_date'][:4] if track['album'].get('release_date') else '',
            'genre': 'Deezer',
            'coverArt': f"al-{track['album']['id']}",
            'size': 1024 * 1024,  # Fake size to make clients happy
            'contentType': 'audio/mpeg',
            'suffix': 'mp3',
            'transcodedContentType': 'audio/mpeg',
            'transcodedSuffix': 'mp3',
            'duration': track['duration'],
            'bitRate': 320,
            'path': f"Deezer/{track['artist']['name']}/{track['album']['title']}/{track['title']}.mp3",
            'albumId': f"al-{track['album']['id']}",
            'artistId': f"ar-{track['artist']['id']}",
            'type': 'music',
            'isVideo': False,
            'created': '2023-01-01T00:00:00.000Z',  # Required field
            'starred': '2023-01-01T00:00:00.000Z',  # Required field
            'playCount': 0,
            'discNumber': 1,
            'userRating': 0,
            'songCount': 1,
            'played': '2023-01-01T00:00:00.000Z',
            'year': 2023,
            'bpm': 0,
            'comment': '',
            'sortName': '',
            'musicBrainzId': '',
            'genres': []
        }
        songs.append(song)
    
    return {"song": songs}

def merge_search_results(navidrome_data, deezer_data):
    """Merge results maintaining Subsonic API structure"""
    result = navidrome_data.copy()
    
    if 'subsonic-response' in result:
        subsonic_response = result['subsonic-response']
        
        if 'searchResult3' in subsonic_response:
            search_result = subsonic_response['searchResult3']
            
            if 'song' in search_result:
                search_result['song'] = search_result['song'] + deezer_data.get('song', [])
            else:
                search_result['song'] = deezer_data.get('song', [])
            
            if 'song' in search_result:
                search_result['totalSongs'] = len(search_result['song'])
                search_result['offset'] = 0
                search_result['songCount'] = len(search_result['song'])
    
    return result

def get_deezer_cover_art(album_id):
    """Get cover art from Deezer - with caching"""
    try:
        # Cache cover art requests
        cache_key = f"cover_{album_id}"
        if cache_key in search_cache:
            cached_data, cache_time = search_cache[cache_key]
            if time.time() - cache_time < CACHE_TIMEOUT:
                return Response(cached_data, content_type='image/jpeg')
        
        album_url = f"https://api.deezer.com/album/{album_id}"
        response = requests.get(album_url, timeout=5)
        
        if response.status_code == 200:
            album_data = response.json()
            
            cover_url = album_data.get('cover_xl') or album_data.get('cover_big')
            
            if cover_url:
                img_response = requests.get(cover_url, stream=True, timeout=5)
                
                if img_response.status_code == 200:
                    # Read and cache the image data
                    img_data = b''.join(img_response.iter_content(chunk_size=8192))
                    search_cache[cache_key] = (img_data, time.time())
                    
                    return Response(
                        img_data,
                        content_type=img_response.headers.get('content-type', 'image/jpeg'),
                        headers={
                            'Cache-Control': 'public, max-age=86400',
                        }
                    )
        
        return return_default_cover()
        
    except Exception as e:
        logger.error(f"Cover art error: {e}")
        return return_default_cover()

def return_default_cover():
    return jsonify({
        "subsonic-response": {
            "status": "failed",
            "version": "1.16.1",
            "error": {
                "code": 70,
                "message": "Cover art not found"
            }
        }
    }), 404

def stream_deezer_track(track_id):
    """Stream Deezer track - with connection pooling"""
    try:
        deezer_id = track_id.replace('deezer_', '')
        
        # Use session for connection reuse
        with requests.Session() as session:
            track_info = session.get(
                f"https://api.deezer.com/track/{deezer_id}",
                timeout=5
            )
            
            if track_info.status_code != 200:
                return jsonify({"error": "Track not found"}), 404
            
            track_data = track_info.json()
            preview_url = track_data.get('preview')
            
            if preview_url:
                stream_resp = session.get(preview_url, stream=True, timeout=10)
                return Response(
                    stream_resp.iter_content(chunk_size=8192),
                    content_type='audio/mpeg',
                    headers={
                        'Content-Type': 'audio/mpeg',
                        'Accept-Ranges': 'bytes',
                        'Cache-Control': 'no-cache'
                    }
                )
            else:
                return jsonify({"error": "No preview available"}), 500
            
    except Exception as e:
        logger.error(f"Streaming error: {e}")
        return jsonify({"error": f"Streaming failed: {str(e)}"}), 500

def trigger_deezer_download(track_id):
    """Trigger download in deemix when user 'loves' a Deezer track"""
    try:
        deezer_id = track_id.replace('deezer_', '')
        
        # Call deemix API to add to download queue
        download_payload = {
            "url": f"https://www.deezer.com/track/{deezer_id}",
            "bitrate": None
        }

        session_cookie = get_deemix_session()
        if not session_cookie:
            logger.error("Failed to get deemix session cookie")
            return jsonify({
                "subsonic-response": {
                    "status": "ok",  # Still return ok to client
                    "version": "1.16.1"
                }
            })
        
        headers = {
            'Content-Type': 'application/json',
            'Cookie': session_cookie,
            'Origin': DEEMIX_URL,
            'Referer': f"{DEEMIX_URL}/",
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) Gecko/20100101 Firefox/145.0',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin'
        }
        
        response = requests.post(
            "{}/api/addToQueue".format(DEEMIX_URL),
            json=download_payload,
            headers=headers,
            timeout=10
        )
        
        if response.status_code in [200, 201]:
            logger.info(f"Successfully queued Deezer track {deezer_id} for download")
            return jsonify({
                "subsonic-response": {
                    "status": "ok",
                    "version": "1.16.1"
                }
            })
        else:
            logger.error(f"Deemix API error: {response.status_code} - {response.text}")
            return jsonify({
                "subsonic-response": {
                    "status": "ok",
                    "version": "1.16.1"
                }
            })
            
    except Exception as e:
        logger.error(f"Download trigger error: {e}")
        return jsonify({
            "subsonic-response": {
                "status": "ok",
                "version": "1.16.1"
            }
        })

def get_deemix_session():
    """Get deemix session cookie by logging in with ARL"""
    try:
        login_url = f"{DEEMIX_URL}/api/loginArl"
        login_payload = {
            "arl": DEEZER_ARL,
            "force": True,
            "child": 0
        }
        
        headers = {
            'Content-Type': 'application/json',
            'Origin': DEEMIX_URL,
            'Referer': f"{DEEMIX_URL}/",
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) Gecko/20100101 Firefox/145.0'
        }
        
        # Use session to maintain cookies
        session = requests.Session()
        response = session.post(
            login_url,
            json=login_payload,
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            # Extract the session cookie from the session
            if session.cookies:
                cookies_dict = session.cookies.get_dict()
                if 'connect.sid' in cookies_dict:
                    cookie_value = cookies_dict['connect.sid']
                    # Format as "connect.sid=value"
                    return f"connect.sid={cookie_value}"
            
            logger.warning("Login successful but no session cookie received")
            return None
        else:
            logger.error(f"Deemix login failed: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"Deemix login error: {e}")
        return None

@app.route('/rest/ping.view')
def ping():
    return jsonify({
        "subsonic-response": {
            "status": "ok",
            "version": "1.16.1",
            "type": "navidrome-deezer-proxy",
            "serverVersion": "0.2.0"
        }
    })

#if __name__ == '__main__':
#    print("=== Navidrome-Deezer Proxy ===")
#    print(f"Navidrome: {NAVIDROME_URL}")
#    print("===============================")
#    app.run(host='0.0.0.0', port=4534, debug=False, threaded=True)

