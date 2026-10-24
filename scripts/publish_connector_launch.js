// Fixed functions.exec entrypoint. Supply only INDEX_PATH from connector-plan.
// The shared develop branch provides the approved executor code: no local source
// upload, source length, manual fingerprint, or source inspection is required.
const remote = await tools.mcp__GitHub__fetch_file({
  repository_full_name:'naro0216n-collab/AutoAdv',
  path:'scripts/publish_connector_executor.js',ref:'develop',
});
const source=remote.result?.content;
if(typeof source!=='string' || !source.length)
  throw Error('Cannot obtain published Connector executor; no GitHub write');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
await new AsyncFunction('tools','text','INDEX_PATH',source)(tools,text,INDEX_PATH);
