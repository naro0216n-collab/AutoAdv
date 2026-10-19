// Fixed functions.exec body. INDEX_PATH is the immutable connector-plan execution index.
// Its parent directory supplies only the deterministic input count; the index contents remain
// authoritative and every packet is checked before any GitHub write.
const root = INDEX_PATH.slice(0, Math.max(INDEX_PATH.lastIndexOf('/'), INDEX_PATH.lastIndexOf('\\')) + 1);
const match = /(?:^|[\\/])inputs-([0-9]{3})[\\/]execution-index-([a-f0-9]{32})\.json$/.exec(INDEX_PATH);
if (!match) throw Error('Invalid transaction index path; no GitHub write');
const [, encodedCount, requestId] = match;
const packetCount = Number(encodedCount);
if (packetCount < 2 || packetCount > 11) throw Error('Invalid packet count: at most 10 create_tree calls; no GitHub write');
const segmentPath = /(?:^|[\\/])segments[\\/]segment-([0-9]{3})[\\/]inputs-[0-9]{3}[\\/]execution-index-[a-f0-9]{32}\.json$/.exec(INDEX_PATH);
const segmentNumber = segmentPath ? Number(segmentPath[1]) : null;
const tempPrefix = 'autoadv-publish-' + requestId + '-';
const tempPattern = new RegExp('^' + tempPrefix + '(?:index|[0-9]+)\\.json$');
const fingerprint = value => {
  let hash = 2166136261;
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code > 127) throw Error('Non-ASCII Connector packet');
    hash = Math.imul(hash ^ code, 16777619) >>> 0;
  }
  return hash;
};
const sha = value => typeof value === 'string' && /^[a-f0-9]{40}$/.test(value);
const inputs = [
  {path:INDEX_PATH, name:'index'},
  ...Array.from({length:packetCount}, (_, i) => ({
    path: root + (i === packetCount - 1 ? 'create-transport-commit.json' :
      'tree-batch-' + String(i).padStart(3, '0') + '.json'), name:String(i),
  })),
];

// A complete, acknowledged deletion is sufficient on the normal path. Scoped
// inventory is reserved for ambiguous writes/deletes or recovery; it cannot
// justify spending tool calls on every successful publication.
async function ownedFiles() {
  const items = [];
  let cursor;
  for (let page = 0; page < 100; page++) {
    const args = {surface:'library',library_path:'/temp',limit:250};
    if (cursor) args.cursor = cursor;
    const response = await tools.files__list(args);
    if (!Array.isArray(response.items)) throw Error('Cannot enumerate temporary Library files');
    for (const item of response.items) {
      if (item.kind === 'file' && typeof item.name === 'string' &&
          tempPattern.test(item.name)) {
        if (!item.library_file_id) throw Error('Temporary file has no Library ID');
        items.push(item.library_file_id);
      }
    }
    if (!response.next_cursor) return [...new Set(items)];
    cursor = response.next_cursor;
  }
  throw Error('Temporary Library listing exceeded page limit');
}

async function deleteIds(ids) {
  for (let start = 0; start < ids.length; start += 20) {
    const group = ids.slice(start, start + 20);
    const result = await tools.files__manage_library({operations:group.map(id => ({
      operation:'delete',target:{kind:'file',library_file_id:id},
    }))});
    if (result.results?.length !== group.length ||
        result.results.some(row => row.status !== 'succeeded'))
      throw Error('Temporary file deletion not acknowledged');
  }
}

async function recoverOwnedFiles() {
  for (let attempt = 0; attempt < 3; attempt++) {
    const ids = await ownedFiles();
    if (!ids.length) return;
    try { await deleteIds(ids); } catch (_) { /* retry only during recovery */ }
  }
  if ((await ownedFiles()).length) throw Error('Temporary file cleanup failed; no GitHub write');
}

// Register the index and original packets together. The trusted index path
// encodes the count, so a separate index upload/read stage is unnecessary.
const tracked = [];
async function registerInputs() {
  for (let start = 0; start < inputs.length;) {
    const first = start === 0;
    const batch = inputs.slice(start, start + (first ? 19 : 20));
    const operations = [
      ...(first ? [{operation:'create_folder',path:'/temp',parents:true}] : []),
      ...batch.map((file, j) => ({
        operation:'upload',container_path:file.path,
        destination_path:'/temp/' + tempPrefix + file.name + '.json',
        overwrite:true, // an interrupted attempt may already have registered this exact input
      })),
    ];
    const response = await tools.files__manage_library({operations});
    const rows = response.results;
    const offset = first ? 1 : 0;
    if (Array.isArray(rows)) {
      for (let j = offset; j < rows.length; j++) {
        if (rows[j]?.library_file_id) tracked.push(rows[j].library_file_id);
      }
    }
    if (!Array.isArray(rows) || rows.length !== operations.length ||
        (first && rows[0].status !== 'succeeded') ||
        batch.some((_, j) => rows[j+offset]?.status !== 'succeeded' ||
          !rows[j+offset].file_id || !rows[j+offset].library_file_id))
      throw Error('Temporary input registration failed');
    batch.forEach((file,j) => {
      file.fileId = rows[j+offset].file_id;
      file.libraryId = rows[j+offset].library_file_id;
    });
    start += batch.length;
  }
}

async function readInputs() {
  const contents = Array(inputs.length);
  for (let first = 0; first < inputs.length; first += 5) {
    let pending = inputs.slice(first, first+5).map((file, i) => ({file,index:first+i}));
    for (let attempt = 0; attempt < 12 && pending.length; attempt++) {
      const response = await tools.files__read({read:pending.map(({file}) => ({
        ref_id:file.fileId,max_lines:1000,
      }))});
      const next = [];
      for (let i = 0; i < pending.length; i++) {
        const {file,index} = pending[i];
        const result = response.results?.[i];
        const parts = result?.content;
        const notVisible = !result || result.warnings?.some(w =>
          typeof w === 'string' && /(?:not (?:currently |yet )?visible|reference is not currently visible)/i.test(w));
        if (notVisible) {
          next.push(pending[i]);
          continue;
        }
        if (result.has_more === true || !Array.isArray(parts) ||
            !parts.every(part => typeof part === 'string'))
          throw Error('Temporary input incomplete or invalid: ' + file.name);
        const raw = parts.join('');
        // Files.read may omit a file's final newline. The trusted index alone
        // authorizes its restoration and checks every packet before GitHub writes.
        if (!raw.startsWith('{') || !raw.trimEnd().endsWith('}'))
          throw Error('Temporary input truncated or malformed: ' + file.name);
        contents[index] = raw;
      }
      pending = next;
      if (pending.length && attempt < 11) {
        // Library registration can precede read visibility. A scoped lookup
        // provides a separate round trip; use a visible backing ID if provided.
        // Never re-upload, relax fingerprints, or retry malformed source data.
        try {
          const listing = await tools.files__list({surface:'library',library_path:'/temp',limit:250});
          for (const {file} of pending) {
            const found = listing.items?.find(item =>
              item.kind === 'file' && item.library_file_id === file.libraryId &&
              item.name === tempPrefix + file.name + '.json');
            if (found?.file_id) file.fileId = found.file_id;
          }
        } catch (_) {
          // A failed lookup does not invalidate the registration receipt.
          // Retry its existing file ID, then fail closed if still invisible.
        }
      }
    }
    if (pending.length) throw Error('Temporary file registration not yet visible');
  }
  return contents;
}

let packets, index, commit;
let verified = false;
try {
  await registerInputs();
  const contents = await readInputs();
  // Source files may have their final newline removed by Files.read. Restore
  // the sole allowed normalization and check each payload against the index.
  index = JSON.parse(contents[0]);
  const segmented = index.version === 2;
  if ((index.version !== 1 && !segmented) || index.request_id !== requestId ||
      index.repository !== 'naro0216n-collab/AutoAdv' ||
      !sha(index.develop_head) || !sha(index.publish_base_head) || !sha(index.publish_base_tree) ||
      !Array.isArray(index.packets) || index.packets.length !== packetCount)
    throw Error('Invalid Connector execution index; no GitHub write');
  const firstIndex = segmented ? index.first_tree_index : 0;
  const treeCount = packetCount - 1;
  if (segmented && (segmentNumber === null || !Number.isSafeInteger(firstIndex) ||
      firstIndex !== segmentNumber * 10 || !Number.isSafeInteger(index.total_tree_count) ||
      index.total_tree_count <= 10 || index.total_tree_count > 256 ||
      firstIndex >= index.total_tree_count || treeCount !== Math.min(10, index.total_tree_count - firstIndex) ||
      !sha(index.start_tree) || !sha(index.end_tree) || !sha(index.final_tree) ||
      typeof index.next_execution_index !== (firstIndex + treeCount < index.total_tree_count ? 'string' : 'object') ||
      (index.next_execution_index === null) !== (firstIndex + treeCount === index.total_tree_count)))
    throw Error('Invalid segmented Connector execution index; no GitHub write');
  if (!segmented && segmentNumber !== null)
    throw Error('Legacy execution index cannot be a segment');
  if (segmented && index.next_execution_index) {
    const nextSegment = String(segmentNumber + 1).padStart(3, '0');
    const suffix = new RegExp('(?:^|[\\/])segments[\\/]segment-' + nextSegment +
      '[\\/]inputs-[0-9]{3}[\\/]execution-index-' + requestId + '\\.json$');
    if (!suffix.test(index.next_execution_index))
      throw Error('Segment continuation index path mismatch; no GitHub write');
  }
  for (let i = 0; i < packetCount; i++) {
    const row = index.packets[i];
    const expectedName = i === packetCount - 1 ? 'create-transport-commit.json' :
      'tree-batch-' + String(i).padStart(3, '0') + '.json';
    if (row.name !== expectedName || !Number.isSafeInteger(row.size) || row.size <= 0 ||
        row.size > 1200000 || !Number.isSafeInteger(row.fnv32) || row.fnv32 < 0 || row.fnv32 > 0xffffffff)
      throw Error('Invalid Connector packet descriptor; no GitHub write');
    let raw = contents[i+1];
    if (raw.length === row.size-1 && fingerprint(raw+'\n') === row.fnv32) raw += '\n';
    if (raw.length !== row.size || fingerprint(raw) !== row.fnv32)
      throw Error('Connector packet fingerprint mismatch; no GitHub write');
    contents[i+1] = raw;
  }
  packets = contents.slice(1).map(raw => JSON.parse(raw));
  let prior = segmented ? index.start_tree : index.publish_base_tree;
  for (let i = 0; i < packets.length - 1; i++) {
    const p = packets[i], a = p.action_args;
    if (JSON.stringify(a).length > 1024*1024 ||
        p.action !== 'GitHub.create_tree' || p.tree_batch_index !== firstIndex + i ||
        a?.repository_full_name !== index.repository || a.base_tree_sha !== prior ||
        !Array.isArray(a.tree_elements) || !sha(p.expected_tree))
      throw Error('Invalid tree packet chain; no GitHub write');
    prior = p.expected_tree;
  }
  if (segmented && prior !== index.end_tree)
    throw Error('Segment end tree mismatch; no GitHub write');
  const last = packets[packets.length - 1];
  commit = last.action_args;
  if (last.action !== 'GitHub.create_commit' || commit?.repository_full_name !== index.repository ||
      commit.tree_sha !== (segmented ? index.final_tree : prior) || commit.parent_sha !== index.publish_base_head ||
      commit.message !== 'Publish transport ' + index.request_id)
    throw Error('Invalid commit packet; no GitHub write');
  verified = true;
} finally {
  // The normal path uses upload acknowledgements and one batched deletion.
  // Only ambiguous or failed calls require a transaction-scoped inventory.
  try {
    if (tracked.length) await deleteIds([...new Set(tracked)]);
    if (!verified) await recoverOwnedFiles();
  } catch (_) {
    await recoverOwnedFiles();
  }
}
if (!verified) throw Error('Connector inputs were not verified');
for (let i = 0; i < packets.length - 1; i++) {
  const result = await tools.mcp__GitHub__create_tree(packets[i].action_args);
  if (result.result?.sha !== packets[i].expected_tree)
    throw Error('GitHub tree SHA mismatch: transaction stopped');
}
if (index.version === 2 && index.next_execution_index) {
  // Persisted Git tree objects are the checkpoint. Only the next immutable index
  // changes; no user-managed SHA, new upload route, or intermediate ref update.
  text(JSON.stringify({stage:'continue',trees_completed:index.first_tree_index + packetCount - 1,
    tree_sha:index.end_tree,next_execution_index:index.next_execution_index}));
} else {
  const created = await tools.mcp__GitHub__create_commit(commit);
  if (!sha(created.result?.sha)) throw Error('GitHub commit SHA missing');
  const updated = await tools.mcp__GitHub__update_ref({
    repository_full_name:index.repository, branch_name:'publish',sha:created.result.sha,force:false,
  });
  if (updated.result?.success !== true) throw Error('Publish ref update failed');
  text(JSON.stringify({stage:'transport-ref-updated',transport_commit:created.result.sha}));
}
