// SEND
function send(){
  let raw=inpEl.value.trim();
  if(!raw&&!atts.length)return;
  let msg=raw;
  if(atts.length)msg+=(raw?'\n\n':'')+atts.map(a=>'```'+a.name+'\n'+a.content+'\n```').join('\n\n');
  atts=[];inpEl.value='';inpEl.style.height='auto';sndBtn.disabled=true;renderAtts();
  addU(raw||'(see attachments)');
  Object.keys(AGENTS).forEach(k=>setSt(k,'idle'));
  curStreams={};
  // Don't create a bubble yet — wait for first status/chunk event
  let active=null;

  function getOrCreate(ak){
    if(!curStreams[ak]){
      // Close any previous open stream
      Object.values(curStreams).forEach(s=>{ if(s&&s!==curStreams[ak]) s.done&&s.done(); });
      curStreams[ak]=streamBub(ak);
    }
    return curStreams[ak];
  }

  fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,sessionId:sid})})
  .then(res=>{
    if(!res.ok)return res.json().catch(()=>({})).then(d=>{
      // Create fallback bubble for error
      const fb=streamBub('manager');
      fb.append('**Error:** '+(d.error||res.statusText));
      fb.done();
      sndBtn.disabled=false;
    });
    const rd=res.body.getReader(),dec=new TextDecoder();let buf='';
    function pump(){return rd.read().then(({done,value})=>{
      if(done){sndBtn.disabled=false;inpEl.focus();loadSess();checkOllama();return;}
      buf+=dec.decode(value,{stream:true});
      buf.split('\n').forEach((line,i,arr)=>{
        if(i===arr.length-1){buf=line;return;}
        if(!line.startsWith('data: '))return;
        try{
          const p=JSON.parse(line.slice(6));
          if(p.type==='status'){
            setSt(p.agent,p.status);
            // Pre-create bubble when agent starts working
            if((p.status==='working'||p.status==='thinking')&&!curStreams[p.agent]){
              getOrCreate(p.agent);
            }
          }
          if(p.type==='chunk'){
            const bub=getOrCreate(p.agent);
            bub.append(p.chunk);
          }
          if(p.type==='tool_call'){
            const bub=getOrCreate(p.agent);
            if(bub.toolCall)bub.toolCall(p.tool,p.params);
          }
          if(p.type==='tool_result'){
            const bub=getOrCreate(p.agent);
            if(bub.toolResult)bub.toolResult(p.tool,p.result);
          }
          if(p.type==='synthesis_chunk'){
            if(!curStreams.syn){
              Object.values(curStreams).forEach(s=>s&&s.done&&s.done());
              curStreams={};
              curStreams.syn=streamBub('manager');
            }
            curStreams.syn.append(p.chunk);
          }
          if(p.type==='done'){
            Object.values(curStreams).forEach(s=>s&&s.done&&s.done());
            curStreams={};
          }
          if(p.type==='error'){
            const bub=getOrCreate(p.agent||'manager');
            bub.append('\n\n**Error:** '+p.message);
            bub.done();
            sndBtn.disabled=false;
          }
        }catch(_){}
      });
      return pump();
    });}
    return pump();
  })
  .catch(()=>{
    const fb=streamBub('manager');
    fb.append('Could not reach the server.');
    fb.done();
    sndBtn.disabled=false;
  });
}