"""Run the single-user local authoring app. Do not bind this MVP publicly."""
import uvicorn
if __name__=='__main__':
    uvicorn.run('studio.app:app',host='127.0.0.1',port=8765,workers=1)
