import os
import time
import logging
from typing import Dict, Any, Optional
import json

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response, JSONResponse, StreamingResponse
import httpx
import asyncio
from urllib.parse import urlencode

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Navidrome-Deezer Proxy")

# Configuration
NAVIDROME_URL = os.getenv("NAVIDROME_URL", "http://localhost:4533")
DEEZER_ARL = os.getenv("DEEZER_ARL")
DEEMIX_URL = os.getenv("DEEMIX_URL")
DEEZER_API_URL = "https://api.deezer.com"

# Cache and async HTTP client
search_cache: Dict[str, Any] = {}
CACHE_TIMEOUT = 300

# Async HTTP client with connection pooling
client = httpx.AsyncClient(timeout=30.0)

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def catch_all(path: str, request: Request):
    """Catch all routes and proxy to Navidrome or handle Deezer requests"""
    start_time = time.time()
    
    try:
        query_params = dict(request.query_params)
        
        # Handle streaming requests
        if path.startswith('rest/stream') or path.startswith('rest/download'):
            track_id = query_params.get('id')
            if track_id and track_id.startswith('deezer_'):
                logger.info(f"Streaming Deezer track: {track_id}")
                return await stream_deezer_track(track_id)
        
        # Handle search requests
        if path.startswith('rest/search'):
            query = query_params.get('query')
            if query:
                cache_key = f"search_{query}"
                if cache_key in search_cache:
                    cached_data, cache_time = search_cache[cache_key]
                    if time.time() - cache_time < CACHE_TIMEOUT:
                        logger.info(f"Using cached search results for: {query}")
                        return JSONResponse(cached_data)
                
                return await handle_search_request(path, request)
        
        # Handle cover art
        if path.startswith('rest/getCoverArt'):
            cover_id = query_params.get('id', '')
            if cover_id and (cover_id.startswith('deezer_') or cover_id.startswith('al-') or cover_id.isdigit()):
                album_id = cover_id.replace('deezer_', '').replace('al-', '')
                return await get_deezer_cover_art(album_id)
        
        # Handle "love" (star) for Deezer tracks - trigger download
        if path.startswith('rest/setRating') or path.startswith('rest/star') or path.startswith('rest/unstar'):
            track_id = query_params.get('id')
            if track_id and track_id.startswith('deezer_'):
                return await trigger_deezer_download(track_id)
        
        # Proxy everything else to Navidrome
        return await proxy_to_navidrome(path, request)
        
    except Exception as e:
        logger.error(f"Error handling request: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        logger.debug(f"Request {path} took {time.time() - start_time:.2f}s")

async def proxy_to_navidrome(path: str, request: Request):
    """Fast async proxy to Navidrome"""
    url = f"{NAVIDROME_URL}/{path}"
    
    # Forward request body if present
    body = await request.body() if request.method in ["POST", "PUT"] else None
    
    response = await client.request(
        method=request.method,
        url=url,
        params=dict(request.query_params),
        headers={k: v for k, v in request.headers.items() if k.lower() != 'host'},
        content=body
    )
    
    return Response(
        content=await response.aread(),
        status_code=response.status_code,
        headers=dict(response.headers)
    )

async def handle_search_request(path: str, request: Request):
    """Handle search requests and merge Deezer results - ASYNC VERSION"""
    try:
        # Run Navidrome and Deezer searches concurrently
        navidrome_task = proxy_to_navidrome(path, request)
        deezer_task = get_deezer_results(request.query_params.get('query'))
        
        navidrome_response, deezer_results = await asyncio.gather(
            navidrome_task, deezer_task, return_exceptions=True
        )
        
        # Handle exceptions
        if isinstance(navidrome_response, Exception):
            raise navidrome_response
        if isinstance(deezer_results, Exception):
            deezer_results = {"song": []}
        
        # If Navidrome response is not JSON, return as-is
        if not isinstance(navidrome_response, JSONResponse):
            return navidrome_response
        
        navidrome_data = await get_json_from_response(navidrome_response)
        
        if deezer_results.get('song'):
            merged_data = merge_search_results(navidrome_data, deezer_results)
            
            # Cache the results
            cache_key = f"search_{request.query_params.get('query')}"
            search_cache[cache_key] = (merged_data, time.time())
            
            return JSONResponse(merged_data)
        else:
            return navidrome_response
            
    except Exception as e:
        logger.error(f"Search error: {e}")
        return await proxy_to_navidrome(path, request)

async def get_json_from_response(response: Response):
    """Extract JSON from response"""
    content = await response.body()
    return json.loads(content)

async def get_deezer_results(query: str):
    """Search Deezer for tracks - ASYNC VERSION"""
    try:
        logger.info(f"Searching Deezer for: {query}")
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{DEEZER_API_URL}/search",
                params={'q': query, 'limit': 250},
                timeout=8.0
            )
            
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
            'size': 1024 * 1024,
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
            'created': '2023-01-01T00:00:00.000Z',
            'starred': '2023-01-01T00:00:00.000Z',
            'playCount': 0,
            'discNumber': 1,
            'userRating': 0,
            'songCount': 1,
            'played': '2023-01-01T00:00:00.000Z',
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

async def get_deezer_cover_art(album_id: str):
    """Get cover art from Deezer - ASYNC VERSION"""
    try:
        cache_key = f"cover_{album_id}"
        if cache_key in search_cache:
            cached_data, cache_time = search_cache[cache_key]
            if time.time() - cache_time < CACHE_TIMEOUT:
                return Response(cached_data, media_type='image/jpeg')
        
        async with httpx.AsyncClient() as client:
            album_url = f"https://api.deezer.com/album/{album_id}"
            response = await client.get(album_url, timeout=5.0)
            
            if response.status_code == 200:
                album_data = response.json()
                cover_url = album_data.get('cover_xl') or album_data.get('cover_big')
                
                if cover_url:
                    img_response = await client.get(cover_url, timeout=5.0)
                    
                    if img_response.status_code == 200:
                        img_data = img_response.content
                        search_cache[cache_key] = (img_data, time.time())
                        
                        return Response(
                            img_data,
                            media_type=img_response.headers.get('content-type', 'image/jpeg'),
                            headers={'Cache-Control': 'public, max-age=86400'}
                        )
        
        return await return_default_cover()
        
    except Exception as e:
        logger.error(f"Cover art error: {e}")
        return await return_default_cover()

async def return_default_cover():
    return JSONResponse({
        "subsonic-response": {
            "status": "failed",
            "version": "1.16.1",
            "error": {
                "code": 70,
                "message": "Cover art not found"
            }
        }
    }, status_code=404)

async def stream_deezer_track(track_id: str):
    """Stream Deezer track - ASYNC VERSION"""
    try:
        deezer_id = track_id.replace('deezer_', '')
        
        async with httpx.AsyncClient() as client:
            track_info = await client.get(f"https://api.deezer.com/track/{deezer_id}", timeout=5.0)
            
            if track_info.status_code != 200:
                return JSONResponse({"error": "Track not found"}, status_code=404)
            
            track_data = track_info.json()
            preview_url = track_data.get('preview')
            
            if preview_url:
                # Stream the audio directly
                return StreamingResponse(
                    client.stream("GET", preview_url),
                    media_type="audio/mpeg",
                    headers={
                        'Accept-Ranges': 'bytes',
                        'Cache-Control': 'no-cache'
                    }
                )
            else:
                return JSONResponse({"error": "No preview available"}, status_code=500)
            
    except Exception as e:
        logger.error(f"Streaming error: {e}")
        return JSONResponse({"error": f"Streaming failed: {str(e)}"}, status_code=500)

async def trigger_deezer_download(track_id: str):
    """Trigger download in deemix when user 'loves' a Deezer track - ASYNC VERSION"""
    try:
        deezer_id = track_id.replace('deezer_', '')
        
        download_payload = {
            "url": f"https://www.deezer.com/track/{deezer_id}",
            "bitrate": None
        }

        session_cookie = await get_deemix_session()
        if not session_cookie:
            logger.error("Failed to get deemix session cookie")
            return JSONResponse({
                "subsonic-response": {
                    "status": "ok",
                    "version": "1.16.1"
                }
            })
        
        headers = {
            'Content-Type': 'application/json',
            'Cookie': session_cookie,
            'Origin': DEEMIX_URL,
            'Referer': f"{DEEMIX_URL}/",
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) Gecko/20100101 Firefox/145.0',
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{DEEMIX_URL}/api/addToQueue",
                json=download_payload,
                headers=headers,
                timeout=10.0
            )
            
            if response.status_code in [200, 201]:
                logger.info(f"Successfully queued Deezer track {deezer_id} for download")
                return JSONResponse({
                    "subsonic-response": {
                        "status": "ok",
                        "version": "1.16.1"
                    }
                })
            else:
                logger.error(f"Deemix API error: {response.status_code} - {response.text}")
                return JSONResponse({
                    "subsonic-response": {
                        "status": "ok",
                        "version": "1.16.1"
                    }
                })
            
    except Exception as e:
        logger.error(f"Download trigger error: {e}")
        return JSONResponse({
            "subsonic-response": {
                "status": "ok",
                "version": "1.16.1"
            }
        })

async def get_deemix_session():
    """Get deemix session cookie by logging in with ARL - ASYNC VERSION"""
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
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                login_url,
                json=login_payload,
                headers=headers,
                timeout=10.0
            )
            
            if response.status_code == 200:
                # Extract cookies from response
                cookies = response.cookies
                if 'connect.sid' in cookies:
                    cookie_value = cookies['connect.sid']
                    return f"connect.sid={cookie_value}"
                
                logger.warning("Login successful but no session cookie received")
                return None
            else:
                logger.error(f"Deemix login failed: {response.status_code} - {response.text}")
                return None
            
    except Exception as e:
        logger.error(f"Deemix login error: {e}")
        return None

@app.get("/rest/ping.view")
async def ping():
    return {
        "subsonic-response": {
            "status": "ok",
            "version": "1.16.1",
            "type": "navidrome-deezer-proxy",
            "serverVersion": "0.3.0"
        }
    }

if __name__ == '__main__':
    import uvicorn
    print("=== Navidrome-Deezer Proxy (FastAPI) ===")
    print(f"Navidrome: {NAVIDROME_URL}")
    print("=========================================")
    uvicorn.run(app, host='0.0.0.0', port=4534, workers=1)