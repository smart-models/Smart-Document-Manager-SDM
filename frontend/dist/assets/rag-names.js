// Mirrors ocrserver/storage_browser.py refine_rag source check and rag_refinement.py output names.
// New saves are json_rag_<job>_<seq>.json; json_rag_refined__ names stay listed
// so archives written before the rename remain browsable and removable.
export const isRagSource = name => /(^|[/.])json_rag\.json$/i.test(name);
export const isRagRefined = name =>
  /(^|[/.])json_rag_refined__[^/]*\.json$/i.test(name)
  // The server writes new names only at the archive root, with no extension prefix.
  || /^json_rag_[A-Za-z0-9_-]+_\d{3,}\.json$/.test(name);
export const isRagListed = name => isRagSource(name) || isRagRefined(name);
