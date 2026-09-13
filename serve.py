import os
import uvicorn

if __name__ == '__main__':
    uvicorn.run('main:app', host='0.0.0.0', port=int(os.getenv('PORT', '8080')),
                workers=1, timeout_keep_alive=5, timeout_graceful_shutdown=120)
