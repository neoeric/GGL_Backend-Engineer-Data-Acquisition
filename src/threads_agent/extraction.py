"""Evidence-only extraction. Unverified Threads structure never proves completeness."""
import json
import copy
import re
from urllib.parse import urlsplit

from .core import field
from .security import check_url, post_identity, Failure


def follower_value(display):
    if isinstance(display, int) and not isinstance(display, bool) and display >= 0:
        return field("ok", {"display": str(display), "exact": display, "approximate": None})
    if not isinstance(display, str):
        return field("unavailable", code="follower_count_missing")
    cleaned = display.strip().replace(",", "")
    if re.fullmatch(r"\d+", cleaned):
        return field("ok", {"display": display, "exact": int(cleaned), "approximate": None})
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMB萬万億亿])", cleaned, re.I)
    if m:
        scale = {"k":1000,"m":1000000,"b":1000000000,"萬":10000,"万":10000,"億":100000000,"亿":100000000}[m[2].lower()]
        result=field("ok", {"display":display,"exact":None,"approximate":round(float(m[1])*scale)})
        result["note"]="Displayed abbreviation; approximate is an estimate, exact is unknown."
        return result
    return field("unconfirmed", code="follower_format_unknown")


def classify(s):
    root = s.get("target_id")
    author = s.get("author")
    nodes = {n["id"]: n for n in s.get("posts", [])}
    if len(nodes) != len(s.get("posts", [])):
        return [], [{"id":n["id"],"classification":"ambiguous_duplicate"} for n in s.get("posts", []) if n["id"] != root], True
    included, decisions, ambiguous = [], [], False
    def ancestry(n):
        if nodes.get(root,{}).get("evidence_conflict"):
            return "ambiguous_evidence_conflict", []
        seen = set()
        chain = []
        while n["id"] != root:
            if n["id"] in seen:
                return "ambiguous_cycle", []
            seen.add(n["id"])
            if n.get("evidence_conflict"):
                return "ambiguous_evidence_conflict", []
            if n.get("reader_branch"):
                return "reader_reply", []
            if n.get("recommended"):
                return "recommended_post", []
            if n.get("author") != author:
                return "reader_reply", []
            chain.append(n)
            parent = n.get("parent")
            if parent not in nodes:
                return "ambiguous_ancestry", []
            n = nodes[parent]
        return "continuation", chain
    for n in s.get("posts", []):
        if n["id"] == root:
            continue
        reason, chain = ancestry(n)
        decisions.append({"id":n["id"],"classification":reason})
        if reason.startswith("ambiguous"):
            ambiguous = True
        elif reason == "continuation":
            # Relationship/order evidence is independent of whether the observed text is complete.
            if any(not isinstance(x.get("order"), int) or not isinstance(x.get("text"), str) for x in chain):
                ambiguous = True
                continue
            included.append({"post_id":n["id"], "url":n["url"], "text":n["text"],
                             "order":n["order"],"parent_post_id":n["parent"],
                             "text_complete":n.get("text_complete") is True,
                             "relationship_inferred":n.get("relationship_inferred"),
                             "order_evidence":n.get("order_evidence")})
            # A sequence-inferred link anywhere in the ancestry is retained evidence, not confirmation.
            if not n.get("text_complete") or any(x.get("relationship_inferred") for x in chain):
                ambiguous = True
    included.sort(key=lambda n:n["order"])
    if len({x["order"] for x in included}) != len(included):
        return [], decisions, True
    return included, decisions, ambiguous


def build_data(s, requested):
    if not s.get("canonical_url"):
        return {f:field("unconfirmed",code="unresolved_target") for f in requested}, []
    nodes = {n["id"]:n for n in s.get("posts", [])}
    root = nodes.get(s.get("target_id"), {})
    data, decisions = {}, []
    for f in requested:
        if f == "display_name":
            data[f] = field("ok",s["display_name"]) if isinstance(s.get("display_name"),str) and s["display_name"] else field("unavailable",code="display_name_missing")
        elif f == "follower_count":
            data[f] = follower_value(s.get("follower_count"))
        elif f == "post_text":
            text = root.get("text")
            data[f] = (field("partial",text,"source_evidence_conflict") if root.get("evidence_conflict") else
                       field("ok",text) if root.get("text_complete") else field("partial",text,"text_completeness_unconfirmed",
                         "Text was obtained, but no complete visible body or explicit source completeness indicator was observed.")) if isinstance(text,str) else field("unavailable",code="post_text_missing")
            if root.get("text_source_difference"):
                data[f]["note"]="Text selected from the visible timestamp-permalink body; differing caption serialization is retained in trace evidence."
        else:
            items, decisions, ambiguous = classify(s)
            # Report the observed thread, without requiring global all-branch termination proof.
            # A known load-more condition, uncertain context or incomplete text is still partial.
            loading=s.get("has_more") is True
            empty_observed=s.get("thread_observed") is True
            complete=not ambiguous and not loading and (bool(items) or empty_observed)
            data[f] = field("ok",items) if complete else field("partial",items,"thread_incomplete",
                "Author continuations retained; text completeness, sequence-inferred parent links, other reply context or unloaded replies remain unconfirmed.") if items else field("unconfirmed",code="thread_context_unconfirmed",
                reason="No author continuation with a verified parent relationship was observed in public data or the visible conversation area.")
            if complete:
                data[f]["note"]="Observed author continuations; additional unloaded content is not verified."
    return data, decisions


def assign_chain_order(posts, root, author):
    """Order only the unique explicit parent chain, never DOM or serialization order."""
    children = {}
    for p in posts:
        p["order"],p["order_evidence"]=None,None
        if p["id"] != root and p.get("author") == author and not p.get("recommended") and not p.get("reader_branch"):
            children.setdefault(p.get("parent"), []).append(p)
    if any(p["id"]==root and p.get("evidence_conflict") for p in posts):
        return
    current, path = root, [root]
    while len(children.get(current, [])) == 1:
        child = children[current][0]
        if child["id"] in path or child.get("evidence_conflict"):
            break
        path = path + [child["id"]]
        child["order"] = len(path)-1
        child["order_evidence"] = {"kind":"explicit_parent_chain", "post_ids":list(path),
                                   "relationship_source":child.get("relationship_source"),
                                   "relationship_inferred":child.get("relationship_inferred")}
        current = child["id"]


def merge_public_snapshots(previous,observed):
    """Monotonic evidence for one verified target; absence is not deletion."""
    merged=copy.deepcopy(previous)
    identity=post_identity(previous.get("canonical_url"))
    if (not identity or post_identity(observed.get("canonical_url"))!=identity or
        observed.get("target_id")!=previous.get("target_id") or observed.get("author")!=previous.get("author")):
        return merged,{"decision":"rejected_target","observed_canonical_url":observed.get("canonical_url"),"conflicts":[]}
    def evidence(post,snapshot):
        return {"captured_at":snapshot.get("captured_at"),"source_url":snapshot.get("canonical_url"),
                **{k:copy.deepcopy(post.get(k)) for k in ["id","author","text","parent","url","text_complete",
                                                          "text_completeness_evidence","relationship_source","relationship_inferred","display_name"]}}
    nodes={p["id"]:p for p in merged.get("posts",[])}
    for p in nodes.values():
        p.setdefault("first_observed_at",previous.get("captured_at"))
        p.setdefault("observations",[evidence(p,previous)])
    conflicts=[]
    added=[]
    for incoming in observed.get("posts",[]):
        incoming_evidence=evidence(incoming,observed)
        if incoming["id"] not in nodes:
            p=copy.deepcopy(incoming)
            p["first_observed_at"]=observed.get("captured_at")
            p["observations"]=[incoming_evidence]
            nodes[p["id"]]=p
            added.append(p["id"])
            continue
        p=nodes[incoming["id"]]
        prior_evidence=copy.deepcopy(p["observations"][0])
        fields=[k for k in ["text","parent","author","url","display_name","text_completeness_evidence"]
                if p.get(k) is not None and incoming.get(k) is not None and p[k]!=incoming[k]]
        if incoming_evidence not in p["observations"]:
            p["observations"].append(incoming_evidence)
        if fields:
            conflict={"post_id":p["id"],"fields":fields,"previous_observation":prior_evidence,"incoming_observation":incoming_evidence}
            p["evidence_conflict"]=True
            p.setdefault("conflicts",[]).append(conflict)
            conflicts.append(conflict)
        else:
            # Supplement missing evidence, retaining the first text, relationship and observation time.
            for k in ["parent","relationship_source","relationship_inferred","display_name","text_completeness_evidence"]:
                if p.get(k) is None and incoming.get(k) is not None:p[k]=copy.deepcopy(incoming[k])
            if incoming.get("text_complete") is True:p["text_complete"]=True
    merged["posts"]=list(nodes.values())
    merged["captured_at"]=observed.get("captured_at") or previous.get("captured_at")
    merged["has_more"]=previous.get("has_more") is True or observed.get("has_more") is True
    merged["thread_observed"]=previous.get("thread_observed") is True or observed.get("thread_observed") is True
    merged.setdefault("merge_conflicts",[]).extend(conflicts)
    # A shorter observation is never evidence that the previously seen conversation ended.
    # Keep the existing boundary evidence only; no new completeness claim is invented by merging.
    assign_chain_order(merged["posts"],merged["target_id"],merged["author"])
    return merged,{"decision":"merged","added_post_ids":added,"retained_post_ids":list(nodes),
                   "conflicts":conflicts,"observation_at":observed.get("captured_at")}


def public_snapshot(doc, source, final, chain, captured_at):
    """Extract caption records with explicit reply_to_post.code parent evidence."""
    s = {"final_url":final,"redirect_chain":chain,"posts":[],"canonical_url":None,
         "boundary_evidence":None,"adapter":"public-json-conservative-v1"}
    candidates = []
    self_relations = {}
    target_identity=post_identity(source) or post_identity(final) or post_identity(doc.get("canonical"))
    documents=[]
    identities={}
    def identity_index(value):
        if isinstance(value,dict):
            user=value.get("user")
            if isinstance(value.get("code"),str) and isinstance(user,dict) and isinstance(user.get("username"),str):
                identity=(user["username"],value["code"])
                for key in ["code","pk","id"]:
                    ref=value.get(key)
                    if isinstance(ref,(str,int)) and not isinstance(ref,bool):identities.setdefault((key,str(ref)),set()).add(identity)
            for child in value.values():identity_index(child)
        elif isinstance(value,list):
            for child in value:identity_index(child)
    for script_index,raw in enumerate(doc.get("scripts",[])):
        try:
            value=json.loads(raw);identity_index(value);documents.append((script_index,value))
        except (ValueError,RecursionError):continue
    def container_identity(value):
        matches=set()
        for key in ["code","pk","id"]:
            ref=value.get(key)
            if isinstance(ref,(str,int)) and not isinstance(ref,bool):
                known=identities.get((key,str(ref)),set())
                if not known:return None
                matches.update(known)
        if len(matches)!=1:return None
        identity=next(iter(matches));user=value.get("user")
        if isinstance(user,dict) and isinstance(user.get("username"),str) and user["username"]!=identity[0]:return None
        return identity
    def walk(value, path, relation=None, excluded=False):
        if isinstance(value, dict):
            relation=relation or self_relations.get(id(value))
            if isinstance(value.get("code"), str) and isinstance(value.get("user"), dict) and isinstance(value.get("caption"), dict):
                u = value["user"]
                handle = u.get("username")
                text = value["caption"].get("text")
                if isinstance(handle,str) and isinstance(text,str):
                    info = value.get("text_post_app_info") or {}
                    parent = info.get("reply_to_post")
                    observed_parent=parent.get("code") if isinstance(parent,dict) else None
                    relationship=path+".text_post_app_info.reply_to_post.code" if isinstance(parent,dict) and isinstance(parent.get("code"),str) else None
                    inferred=None
                    if relation and info.get("is_reply") is True and isinstance(info.get("reply_to_author"),dict) and info["reply_to_author"].get("username")==relation["author"]:
                        if observed_parent is not None and observed_parent!=relation["parent"]:
                            relation={**relation,"conflict":True}
                        else:
                            # An explicit matching reply_to_post confirms the link; only a missing one is inferred.
                            if observed_parent is None:inferred=relation.get("inferred")
                            observed_parent,relationship=relation["parent"],relation["source"]
                    candidates.append({"id":value["code"],"author":handle,"text":text,
                                       "url":f"https://www.threads.com/@{handle}/post/{value['code']}",
                                       "parent":observed_parent,"relationship_source":relationship,"relationship_inferred":inferred,
                                       "evidence_conflict":bool(relation and relation.get("conflict")),"recommended":excluded,
                                       "source_path":path,"self_thread_context":relation,
                                       "is_reply_observed":info.get("is_reply") if isinstance(info.get("is_reply"),bool) else None,
                                       "reply_to_author_observed":info["reply_to_author"].get("username") if isinstance(info.get("reply_to_author"),dict) else None,
                                       "reader_branch":bool(target_identity and ".direct_replies." in path and
                                           isinstance(info.get("reply_to_author"),dict) and isinstance(info["reply_to_author"].get("username"),str) and
                                           info["reply_to_author"]["username"]!=target_identity[0]),
                                       "order":None, "text_complete":value.get("is_caption_truncated") is False,
                                       "text_completeness_evidence":value.get("is_caption_truncated") if isinstance(value.get("is_caption_truncated"),bool) else None,
                                       "display_name":u.get("full_name")})
            # An anchored self_thread connection plus matching reply target is
            # observed relationship evidence. Its first edge replies to this post;
            # later edges still need their own parent evidence.
            children=(value.get("text_post_app_info") or {}).get("self_thread") if isinstance(value.get("text_post_app_info"),dict) else None
            edges=(children.get("posts") or {}).get("edges") if isinstance(children,dict) else None
            if isinstance(edges,list) and edges:
                # Public hydration can put identity and self_thread in separate scripts.
                # Join only by an exact, unique code/pk/id observed in a named post record.
                identity=container_identity(value)
                if identity:
                    # edges[0] replies to the anchored container. Later edges carry no explicit
                    # reply_to_post in public data; their parent is taken from the previous edge
                    # and marked inferred, so the result stays partial rather than dropping them.
                    previous_code=identity[1]
                    for index,edge in enumerate(edges):
                        node=edge.get("node") if isinstance(edge,dict) else None
                        if not isinstance(node,dict) or previous_code is None:break
                        self_relations[id(node)]={"parent":previous_code,"author":identity[0],
                            "source":f"{path}.text_post_app_info.self_thread.posts.edges[{index}].node",
                            "inferred":"self_thread_sequence" if index else None}
                        previous_code=node.get("code") if isinstance(node.get("code"),str) else None
            for k,v in value.items():
                walk(v,path+"."+k,None,excluded or k in {"linked_inline_media","relatedPosts"})
        elif isinstance(value,list):
            for i,v in enumerate(value):
                walk(v,path+f"[{i}]",excluded=excluded)
    for script_index,value in documents:
        try:
            walk(value,f"scripts[{script_index}]")
        except RecursionError:
            continue
    # A visible permalink and explicit reply-context link are enough; internal parent IDs are optional.
    for card in doc.get("visible_posts",[]):
        if not isinstance(card,dict):continue
        identity=post_identity(card.get("url"))
        if not identity or not isinstance(card.get("text"),str):continue
        matching=[x for x in candidates if (x["author"],x["id"])==identity]
        parent=post_identity(card.get("parent_url"))
        if matching:
            for existing in matching:
                if card.get("body_evidence")=="permalink_time_scoped_visible_body" and card.get("truncated") is False:
                    if existing["text"]!=card["text"]:
                        existing["text_source_difference"]={"caption_text":existing["text"],"visible_text":card["text"],
                            "selected_source":"permalink_time_scoped_visible_body","inline_image_count":card.get("inline_image_count",0)}
                    existing.update(text=card["text"],text_source="permalink_time_scoped_visible_body")
                if parent:
                    if card["text"]!=existing["text"] or existing.get("parent") not in {None,parent[1]}:
                        existing["evidence_conflict"]=True
                        existing.setdefault("conflicts",[]).append({"kind":"visible_reply_context_conflict",
                            "json_parent":existing.get("parent"),"dom_parent":parent[1],
                            "text_matches":card["text"]==existing["text"]})
                        existing.update(parent=None,relationship_source=None)
                    elif not existing.get("evidence_conflict") and existing.get("parent") is None:
                        existing.update(parent=parent[1],relationship_source="visible_reply_context_link")
                if card.get("truncated") is True:
                    existing.update(text_complete=False,text_completeness_evidence=True,dom_truncated=True)
                elif card.get("truncated") is False and card["text"]==existing["text"] and existing.get("text_completeness_evidence") is None:
                    existing.update(text_complete=True,text_completeness_evidence=False)
            continue
        candidates.append({"id":identity[1],"author":identity[0],"url":card["url"],"text":card["text"],
            "parent":parent[1] if parent else None,"relationship_source":"visible_reply_context_link" if parent else None,
            "order":None,"text_complete":card.get("truncated") is False,
            "text_completeness_evidence":None,"display_name":card.get("display_name")})
    canonical = doc.get("canonical")
    try:
        canonical = check_url(canonical) if canonical else None
    except Failure:
        canonical = None
    expected = post_identity(source) or post_identity(final) or post_identity(canonical)
    if not canonical and expected:
        canonical=next((x["url"] for x in candidates if (x["author"],x["id"])==expected),None)
    # For share pages, canonical metadata AND matching primary public record are required.
    if not expected or not post_identity(canonical) or post_identity(canonical) != expected:
        return s
    matches = [x for x in candidates if (x["author"],x["id"]) == expected]
    if not matches or len({x["text"] for x in matches}) != 1:
        return s
    root = matches[0]
    # Deduplicate repeated serialization of exactly the same public post.
    unique = {}
    for c in candidates:
        if c["id"] in unique:
            previous=unique[c["id"]]
            if any(previous.get(k)!=c.get(k) for k in ["author","text","parent","text_complete"]):
                return s
        else:
            unique[c["id"]] = c
    assign_chain_order(list(unique.values()),root["id"],root["author"])
    thread_seen=doc.get("thread_observed") is True or any(x["id"]!=root["id"] and x.get("parent")==root["id"] for x in unique.values())
    s.update(canonical_url=canonical, target_id=root["id"],author=root["author"],thread_observed=thread_seen,
             has_more=doc.get("has_more") is True,
             display_name=root["display_name"],posts=list(unique.values()),captured_at=captured_at)
    if not post_identity(source):
        s["redirect_chain"] = chain + [{"url":canonical,"kind":"canonical_metadata_verified_by_post"}]
    return s
