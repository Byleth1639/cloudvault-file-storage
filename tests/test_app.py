"""API smoke/integration tests using an isolated, local SQLite database."""
import io
import os
import tempfile
from pathlib import Path

TEMP = tempfile.TemporaryDirectory(prefix='cloudvault-test-')
os.environ['DATA_DIR'] = TEMP.name
os.environ['DATABASE_URL'] = f"sqlite:///{Path(TEMP.name) / 'test.db'}"
os.environ['STORAGE_BACKEND'] = 'local'
os.environ['COOKIE_SECURE'] = 'false'

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def signup(client, email='alex@example.com'):
    r = client.post('/api/auth/register', json={'email': email, 'name': 'Alex Tester', 'password': 'super-strong-pw'})
    assert r.status_code == 201, r.text
    return r.json()['csrf']


def upload(client, csrf, filename='notes.txt', content=b'hello cloud'):
    return client.post('/api/files', files={'file': (filename, io.BytesIO(content), 'text/plain')}, headers={'x-csrf-token': csrf})


def test_complete_file_lifecycle():
    with TestClient(app) as c:
        csrf = signup(c)
        assert c.get('/api/me').json()['user']['name'] == 'Alex Tester'
        assert c.get('/api/health').json()['status'] == 'ok'
        assert 'CloudVault' in c.get('/').text
        assert 'Your files' in c.get('/static/index.html').text or 'CloudVault' in c.get('/static/index.html').text
        folder = c.post('/api/folders', json={'name':'Internship files'},headers={'x-csrf-token':csrf})
        assert folder.status_code == 201, folder.text
        folder_id = folder.json()['id']
        r = c.post('/api/files',files={'file':('notes.txt',io.BytesIO(b'hello cloud'),'text/plain')},data={'folder_id':folder_id},headers={'x-csrf-token':csrf})
        assert r.status_code == 201, r.text
        file_id = r.json()['id']
        assert r.json()['sha256']
        listed = c.get('/api/items',params={'folder_id':folder_id}).json()
        assert len(listed['files']) == 1
        assert c.get(f'/api/files/{file_id}/content').content == b'hello cloud'
        rename = c.patch(f'/api/files/{file_id}',json={'name':'renamed.txt','starred':True},headers={'x-csrf-token':csrf})
        assert rename.status_code == 200 and rename.json()['starred']
        assert c.get('/api/items',params={'view':'starred'}).json()['files'][0]['name'] == 'renamed.txt'
        sharing = c.post(f'/api/files/{file_id}/shares',json={'days':7},headers={'x-csrf-token':csrf})
        assert sharing.status_code == 201, sharing.text
        token = sharing.json()['url'].split('/s/')[1]
        assert c.get(f'/api/public/{token}').json()['name'] == 'renamed.txt'
        assert c.get(f'/api/public/{token}/download').content == b'hello cloud'
        share_id = sharing.json()['id']
        assert c.delete(f'/api/shares/{share_id}',headers={'x-csrf-token':csrf}).status_code == 200
        assert c.get(f'/api/public/{token}').status_code == 404
        assert c.delete(f'/api/files/{file_id}',headers={'x-csrf-token':csrf}).status_code == 200
        assert len(c.get('/api/items',params={'view':'trash'}).json()['files']) == 1
        assert c.get(f'/api/files/{file_id}/content').status_code == 404
        assert c.post(f'/api/files/{file_id}/restore',headers={'x-csrf-token':csrf}).status_code == 200
        assert c.patch(f'/api/files/{file_id}',json={'folder_id':None},headers={'x-csrf-token':csrf}).status_code == 200
        assert c.delete(f'/api/folders/{folder_id}',headers={'x-csrf-token':csrf}).status_code == 200
        assert c.delete(f'/api/files/{file_id}',headers={'x-csrf-token':csrf}).status_code == 200
        assert c.delete(f'/api/files/{file_id}/permanent',headers={'x-csrf-token':csrf}).status_code == 200
        assert c.get('/api/dashboard').json()['total_files'] == 0
        assert c.post('/api/auth/logout',headers={'x-csrf-token':csrf}).status_code == 200
        assert c.get('/api/me').status_code == 401


def test_auth_ownership_csrf_and_folder_cycle():
    with TestClient(app) as owner, TestClient(app) as stranger:
        csrf = signup(owner, 'owner@example.com')
        theirs = signup(stranger,'stranger@example.com')
        folder1 = owner.post('/api/folders',json={'name':'Private'},headers={'x-csrf-token':csrf}).json()['id']
        folder2 = owner.post('/api/folders',json={'name':'Nested','parent_id':folder1},headers={'x-csrf-token':csrf}).json()['id']
        assert owner.patch(f'/api/folders/{folder1}',json={'parent_id':folder2},headers={'x-csrf-token':csrf}).status_code == 400
        f=upload(owner,csrf).json()['id']
        assert stranger.get(f'/api/files/{f}/content').status_code == 404
        assert stranger.patch(f'/api/files/{f}',json={'name':'hacked.txt'},headers={'x-csrf-token':theirs}).status_code == 404
        assert owner.delete(f'/api/files/{f}').status_code == 403
        assert owner.post('/api/folders',json={'name':'Private'},headers={'x-csrf-token':csrf}).status_code == 409
        assert owner.delete(f'/api/folders/{folder1}',headers={'x-csrf-token':csrf}).status_code == 409


def test_limits_validation_and_demo():
    with TestClient(app) as c:
        assert c.get('/api/items').status_code == 401
        demo = c.post('/api/auth/demo')
        assert demo.status_code == 200
        csrf = demo.json()['csrf']
        assert c.post('/api/folders',json={'name':'   '},headers={'x-csrf-token':csrf}).status_code == 400
        assert upload(c,csrf,filename='demo.csv',content=b'a,b\n1,2').status_code == 201
        assert c.get('/api/search',params={'q':'demo'}).json()['files'][0]['name'] == 'demo.csv'
        assert c.post('/api/auth/login',json={'email':'nobody@example.com','password':'wrong'}).status_code == 401
