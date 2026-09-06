import {useEffect,useRef} from 'react';
import {loadArtwork,VectorBoard} from './detailed-board.mjs';

/** Supply manifestUrl; use onReady(board) to wire an external palette/toolbar. */
export default function DetailedArtwork({manifestUrl,mode='number',onReady,onChange}) {
  const svgRef=useRef(null), ready=useRef(onReady), change=useRef(onChange);
  ready.current=onReady;change.current=onChange;
  useEffect(()=>{
    const abort=new AbortController();let board,disposed=false;
    loadArtwork(manifestUrl,{signal:abort.signal}).then(bundle=>{
      if(disposed)return;
      board=new VectorBoard(svgRef.current,bundle,{mode,onChange:(s,r)=>change.current?.(s,r)});
      ready.current?.(board);
    }).catch(error=>{if(error.name!=='AbortError')change.current?.({error:error.message},'error');});
    return ()=>{disposed=true;abort.abort();board?.destroy();};
  },[manifestUrl,mode]);
  return <svg ref={svgRef} style={{width:'100%',height:'100%',touchAction:'none',display:'block'}}/>;
}
