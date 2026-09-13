"use client";

/**
 * Task 32 — Semantic Object & Layer Inspector.
 *
 * Exposes objects.json in an artist-friendly tree & inspector card:
 *  - Hierarchical tree view (nested parent-child relationships).
 *  - Selection highlights visual and gameplay geometry on canvas.
 *  - Quick actions: select all regions, hide/isolate (DOM-level UI state).
 *  - Edit actions: rename, reparent (with cycle check), lock/unlock,
 *    detail priority (subdivision metadata), layer ordering.
 *  - Targeted AI regeneration (disabled when object is locked).
 */

import { useMemo, useState } from "react";
import {
  ArrowDownToLine,
  ArrowUpToLine,
  Box,
  Check,
  ChevronDown,
  ChevronRight,
  Eye,
  EyeOff,
  Focus,
  Layers,
  Lock,
  MoveDown,
  MoveUp,
  Pencil,
  Sparkles,
  Unlock,
  X,
} from "lucide-react";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import type { SemanticObject } from "@/lib/studio-api";
import { computeObjectVisibility } from "@/lib/detailed-board";
import { useStudioContext } from "./use-studio";

const DETAIL_WEIGHT_PRESETS = [
  { label: "Low", value: 0.5, desc: "Fewer, larger tap regions" },
  { label: "Medium", value: 1.0, desc: "Balanced standard difficulty" },
  { label: "High", value: 2.0, desc: "Dense challenge subdivision" },
  { label: "Focal", value: 4.0, desc: "Finely divided focal point" },
] as const;

export function ObjectInspector() {
  const {
    bundle,
    busy,
    selectedObjectId,
    setSelectedObjectId,
    hiddenObjectIds,
    isolatedObjectId,
    toggleHideObject,
    toggleIsolateObject,
    selectAllObjectRegions,
    updateObject,
    activeSession,
    regenerateObject,
  } = useStudioContext();

  const objects = useMemo(() => bundle?.objects?.objects || [], [bundle]);

  // Task 32 R2/R4 — subtree-aware effective visibility (hide/isolate on a
  // parent covers its descendants) and the revision→draft linkage gate.
  const effectiveHidden = useMemo(
    () => computeObjectVisibility(objects, hiddenObjectIds, isolatedObjectId),
    [objects, hiddenObjectIds, isolatedObjectId]
  );
  const derivedSessionId = bundle?.manifest.generation?.sessionId;
  const linkedToRevision =
    !!derivedSessionId &&
    !!activeSession &&
    activeSession.id === derivedSessionId &&
    activeSession.status !== "committed";
  const [regenDialogOpen, setRegenDialogOpen] = useState(false);
  const [regenInstructions, setRegenInstructions] = useState("");
  const [regenPaidConsent, setRegenPaidConsent] = useState(false);

  // Compute region counts per object
  const regionCounts = useMemo(() => {
    const map = new Map<string, number>();
    if (!bundle?.geometry.regions) return map;
    for (const r of bundle.geometry.regions) {
      if (r.objectId && r.objectId !== "unassigned") {
        map.set(r.objectId, (map.get(r.objectId) || 0) + 1);
      }
    }
    return map;
  }, [bundle]);

  // Build tree hierarchy: root objects and children map
  const { roots, childrenMap, allDescendantsMap } = useMemo(() => {
    const known = new Set(objects.map((o) => o.id));
    const children = new Map<string, SemanticObject[]>();
    const rootList: SemanticObject[] = [];

    for (const obj of objects) {
      if (obj.parentId && known.has(obj.parentId) && obj.parentId !== obj.id) {
        const arr = children.get(obj.parentId) || [];
        arr.push(obj);
        children.set(obj.parentId, arr);
      } else {
        rootList.push(obj);
      }
    }

    // Precalculate descendants for cycle prevention
    const descendants = new Map<string, Set<string>>();
    for (const obj of objects) {
      const set = new Set<string>();
      const queue = [...(children.get(obj.id) || [])];
      while (queue.length > 0) {
        const next = queue.shift()!;
        if (!set.has(next.id)) {
          set.add(next.id);
          queue.push(...(children.get(next.id) || []));
        }
      }
      descendants.set(obj.id, set);
    }

    return { roots: rootList, childrenMap: children, allDescendantsMap: descendants };
  }, [objects]);

  const selectedObject = useMemo(
    () => objects.find((o) => o.id === selectedObjectId) || null,
    [objects, selectedObjectId]
  );

  // Local editing states
  const [editingName, setEditingName] = useState(false);
  const [nameInput, setNameInput] = useState("");
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

  const toggleExpand = (id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handleStartRename = (obj: SemanticObject) => {
    setNameInput(obj.name);
    setEditingName(true);
  };

  const handleSaveRename = async (obj: SemanticObject) => {
    const trimmed = nameInput.trim();
    if (!trimmed || trimmed === obj.name) {
      setEditingName(false);
      return;
    }
    try {
      await updateObject({ object_id: obj.id, name: trimmed });
      setEditingName(false);
      toast("Object renamed.");
    } catch (e) {
      toast((e as Error).message);
    }
  };

  const handleReparent = async (obj: SemanticObject, newParent: string) => {
    const targetParentId = newParent === "__none__" ? "" : newParent;
    try {
      await updateObject({ object_id: obj.id, parent_id: targetParentId });
      toast("Object hierarchy updated.");
    } catch (e) {
      toast((e as Error).message);
    }
  };

  const handleToggleLock = async (obj: SemanticObject) => {
    const currentLocked = !!obj.generation?.locked;
    try {
      await updateObject({ object_id: obj.id, locked: !currentLocked });
      toast(currentLocked ? `Unlocked "${obj.name}".` : `Locked "${obj.name}".`);
    } catch (e) {
      toast((e as Error).message);
    }
  };

  const handleDetailWeight = async (obj: SemanticObject, weight: number) => {
    try {
      await updateObject({ object_id: obj.id, detail_weight: weight });
      toast(`Detail priority set to ${weight}x.`);
    } catch (e) {
      toast((e as Error).message);
    }
  };

  const handleLayerOrder = async (
    obj: SemanticObject,
    action: "bring_to_front" | "send_to_back" | "above" | "below",
    targetId?: string
  ) => {
    try {
      await updateObject({
        object_id: obj.id,
        order_action: action,
        target_object_id: targetId,
      });
      toast(`Moved layer ${action.replace(/_/g, " ")}.`);
    } catch (e) {
      toast((e as Error).message);
    }
  };

  const handleRegenerate = async (obj: SemanticObject) => {
    // Task 32 review R4: targeted regeneration must run against an AI draft
    // DERIVED FROM THE DISPLAYED REVISION — an unrelated active draft is
    // never reused, and the paid provider call happens only after the
    // explicit confirmation below (zero calls until confirmed).
    if (obj.generation?.locked) {
      toast("Object is locked. Unlock it before regenerating.");
      return;
    }
    if (!linkedToRevision) {
      toast("No AI draft derived from this revision is active.");
      return;
    }
    if (!regenPaidConsent) {
      toast("Confirm the paid provider request first.");
      return;
    }
    try {
      await regenerateObject(obj.id, regenInstructions, true);
      toast(`Regenerating "${obj.name}"...`);
      setRegenDialogOpen(false);
      setRegenInstructions("");
      setRegenPaidConsent(false);
    } catch (e) {
      toast((e as Error).message);
    }
  };

  // Render tree node recursively
  const renderTreeNode = (obj: SemanticObject, depth = 0) => {
    const hasChildren = (childrenMap.get(obj.id) || []).length > 0;
    const isExpanded = expandedIds.has(obj.id);
    const isSelected = selectedObjectId === obj.id;
    const isHidden = effectiveHidden.has(obj.id);
    const isIsolated = isolatedObjectId === obj.id;
    const regCount = regionCounts.get(obj.id) || 0;
    const shpCount = obj.shapeIds?.length || 0;
    const isLocked = !!obj.generation?.locked;

    return (
      <div key={obj.id} className="select-none">
        <div
          onClick={() => setSelectedObjectId(obj.id)}
          className={`group flex items-center justify-between gap-1 rounded-md px-1.5 py-1 text-xs transition-colors cursor-pointer ${
            isSelected
              ? "bg-[#e5f3ed] text-[#087f74] font-semibold shadow-[0_1px_3px_rgba(8,127,116,0.1)]"
              : "text-[#183837] hover:bg-[#f0f4f1]"
          }`}
          style={{ paddingLeft: `${depth * 14 + 6}px` }}
        >
          <div className="flex items-center gap-1.5 min-w-0 overflow-hidden">
            {hasChildren ? (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  toggleExpand(obj.id);
                }}
                className="size-4 p-0 flex items-center justify-center text-[#778481] hover:text-[#183837]"
                aria-label={isExpanded ? "Collapse" : "Expand"}
              >
                {isExpanded ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
              </button>
            ) : (
              <span className="size-4 flex items-center justify-center text-[#a1aca8]">
                <Box className="size-3 opacity-60" />
              </span>
            )}

            <span className="truncate" title={obj.name}>
              {obj.name}
            </span>

            {isLocked && (
              <span title="Locked against AI regeneration">
                <Lock className="size-3 shrink-0 text-[#087f74]" />
              </span>
            )}
          </div>

          <div className="flex items-center gap-1 shrink-0">
            <span className="text-[10px] text-[#778481] opacity-75 font-mono">
              {regCount}r · {shpCount}s
            </span>

            {/* Visibility Toggle */}
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                toggleHideObject(obj.id);
              }}
              className={`size-5 flex items-center justify-center rounded p-0 text-[#778481] hover:text-[#183837] ${
                isHidden ? "text-amber-600 bg-amber-50" : "opacity-0 group-hover:opacity-100"
              }`}
              title={isHidden ? "Unhide" : "Hide in view"}
            >
              {isHidden ? <EyeOff className="size-3" /> : <Eye className="size-3" />}
            </button>

            {/* Isolate Toggle */}
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                toggleIsolateObject(obj.id);
              }}
              className={`size-5 flex items-center justify-center rounded p-0 ${
                isIsolated
                  ? "text-[#087f74] bg-[#cce7dc] opacity-100"
                  : "text-[#778481] opacity-0 group-hover:opacity-100 hover:text-[#183837]"
              }`}
              title={isIsolated ? "Exit Isolation" : "Isolate object"}
            >
              <Focus className="size-3" />
            </button>
          </div>
        </div>

        {hasChildren && isExpanded && (
          <div className="space-y-0.5">
            {(childrenMap.get(obj.id) || []).map((child) => renderTreeNode(child, depth + 1))}
          </div>
        )}
      </div>
    );
  };

  if (!bundle) return null;

  return (
    <div className="rp-wide rounded-[10px] border border-[#d6dfdc] bg-white p-3 shadow-xs">
      <div className="mb-2.5 flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <Layers className="size-4 text-[#087f74]" />
          <h3 className="text-[13px] font-bold text-[#183837]">Objects &amp; Layers</h3>
        </div>
        <span className="rounded-full bg-[#f0f4f1] px-2 py-0.5 text-[10px] font-semibold text-[#526460]">
          {objects.length} objects
        </span>
      </div>

      {objects.length === 0 ? (
        <div className="rounded-md border border-dashed border-[#d6dfdc] p-4 text-center">
          <Box className="mx-auto size-6 text-[#9eb1ac] opacity-50" />
          <p className="mt-1 text-xs font-medium text-[#526460]">No semantic objects</p>
          <p className="mt-0.5 text-[10px] text-[#778481]">
            Objects are defined during AI creation or Convert Artwork.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {/* Object Tree */}
          <div className="max-h-56 overflow-y-auto rounded-md border border-[#e1e8e5] bg-[#fbfcfb] p-1.5 studio-scroll space-y-0.5">
            {roots.map((root) => renderTreeNode(root, 0))}
          </div>

          {/* Selected Object Details Card */}
          {selectedObject ? (
            <div className="rounded-lg border border-[#cce7dc] bg-[#f7fbf9] p-3 space-y-3 text-xs">
              {/* Header: Name and ID */}
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  {editingName ? (
                    <div className="flex items-center gap-1">
                      <Input
                        value={nameInput}
                        onChange={(e) => setNameInput(e.target.value)}
                        className="h-7 text-xs bg-white"
                        autoFocus
                        onKeyDown={(e) => {
                          if (e.key === "Enter") void handleSaveRename(selectedObject);
                          if (e.key === "Escape") setEditingName(false);
                        }}
                      />
                      <Button
                        size="icon"
                        variant="ghost"
                        className="size-7 text-[#087f74]"
                        onClick={() => void handleSaveRename(selectedObject)}
                      >
                        <Check className="size-3.5" />
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        className="size-7 text-[#778481]"
                        onClick={() => setEditingName(false)}
                      >
                        <X className="size-3.5" />
                      </Button>
                    </div>
                  ) : (
                    <div className="flex items-center gap-1.5">
                      <strong className="text-sm font-bold text-[#183837] truncate">{selectedObject.name}</strong>
                      <button
                        type="button"
                        onClick={() => handleStartRename(selectedObject)}
                        className="text-[#778481] hover:text-[#183837] p-0.5"
                        title="Rename object"
                      >
                        <Pencil className="size-3" />
                      </button>
                    </div>
                  )}
                  <p className="font-mono text-[10px] text-[#778481] truncate">{selectedObject.id}</p>
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  <Button
                    size="sm"
                    variant={selectedObject.generation?.locked ? "secondary" : "outline"}
                    className="h-7 px-2 text-[11px] gap-1"
                    onClick={() => void handleToggleLock(selectedObject)}
                    disabled={busy}
                  >
                    {selectedObject.generation?.locked ? (
                      <>
                        <Lock className="size-3 text-[#087f74]" /> Locked
                      </>
                    ) : (
                      <>
                        <Unlock className="size-3 text-[#778481]" /> Unlocked
                      </>
                    )}
                  </Button>
                </div>
              </div>

              {/* Roles and Counts */}
              <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                {selectedObject.role && (
                  <span className="rounded bg-[#edf3ef] px-1.5 py-0.5 font-medium text-[#485e58]">
                    Role: {selectedObject.role}
                  </span>
                )}
                {selectedObject.type && (
                  <span className="rounded bg-[#edf3ef] px-1.5 py-0.5 font-medium text-[#485e58]">
                    Type: {selectedObject.type}
                  </span>
                )}
                <span className="rounded bg-white px-1.5 py-0.5 text-[#526460] border border-[#e1e8e5]">
                  {regionCounts.get(selectedObject.id) || 0} gameplay regions
                </span>
                <span className="rounded bg-white px-1.5 py-0.5 text-[#526460] border border-[#e1e8e5]">
                  {selectedObject.shapeIds?.length || 0} visual shapes
                </span>
              </div>

              {/* Geometry Selection CTA */}
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  className="flex-1 h-7 text-xs bg-white hover:bg-[#e5f3ed] hover:text-[#087f74]"
                  onClick={() => selectAllObjectRegions(selectedObject.id)}
                >
                  Select All Regions ({regionCounts.get(selectedObject.id) || 0})
                </Button>
              </div>

              {/* Hierarchy / Reparenting */}
              <div className="space-y-1">
                <Label className="text-[10px] text-[#657671] uppercase font-semibold tracking-wider">
                  Parent Object
                </Label>
                <Select
                  value={selectedObject.parentId || "__none__"}
                  onValueChange={(val) => void handleReparent(selectedObject, val)}
                  disabled={busy}
                >
                  <SelectTrigger className="h-7 text-xs bg-white">
                    <SelectValue placeholder="Top Level (No Parent)" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__">Top Level (No Parent)</SelectItem>
                    {objects
                      .filter(
                        (candidate) =>
                          candidate.id !== selectedObject.id &&
                          !allDescendantsMap.get(selectedObject.id)?.has(candidate.id)
                      )
                      .map((candidate) => (
                        <SelectItem key={candidate.id} value={candidate.id}>
                          {candidate.name} ({candidate.id})
                        </SelectItem>
                      ))}
                  </SelectContent>
                </Select>
              </div>

              {/* Layer Ordering Controls */}
              <div className="space-y-1">
                <Label className="text-[10px] text-[#657671] uppercase font-semibold tracking-wider">
                  Layer Ordering
                </Label>
                <div className="grid grid-cols-4 gap-1">
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 p-0 text-[10px] bg-white gap-0.5"
                    onClick={() => void handleLayerOrder(selectedObject, "bring_to_front")}
                    disabled={busy}
                    title="Bring to front of all layers"
                  >
                    <ArrowUpToLine className="size-3" /> Front
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 p-0 text-[10px] bg-white gap-0.5"
                    onClick={() => {
                      const idx = objects.findIndex((o) => o.id === selectedObject.id);
                      if (idx < objects.length - 1) {
                        void handleLayerOrder(selectedObject, "above", objects[idx + 1].id);
                      }
                    }}
                    disabled={busy || objects.findIndex((o) => o.id === selectedObject.id) >= objects.length - 1}
                    title="Move layer up"
                  >
                    <MoveUp className="size-3" /> Up
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 p-0 text-[10px] bg-white gap-0.5"
                    onClick={() => {
                      const idx = objects.findIndex((o) => o.id === selectedObject.id);
                      if (idx > 0) {
                        void handleLayerOrder(selectedObject, "below", objects[idx - 1].id);
                      }
                    }}
                    disabled={busy || objects.findIndex((o) => o.id === selectedObject.id) <= 0}
                    title="Move layer down"
                  >
                    <MoveDown className="size-3" /> Down
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 p-0 text-[10px] bg-white gap-0.5"
                    onClick={() => void handleLayerOrder(selectedObject, "send_to_back")}
                    disabled={busy}
                    title="Send to back of all layers"
                  >
                    <ArrowDownToLine className="size-3" /> Back
                  </Button>
                </div>
              </div>

              {/* Detail Priority / Subdivision metadata */}
              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <Label className="text-[10px] text-[#657671] uppercase font-semibold tracking-wider">
                    Detail Priority
                  </Label>
                  <span className="font-mono text-[10px] font-bold text-[#087f74]">
                    {selectedObject.subdivision?.detailWeight ?? 1.0}x
                  </span>
                </div>
                <div className="grid grid-cols-4 gap-1">
                  {DETAIL_WEIGHT_PRESETS.map((p) => {
                    const active = (selectedObject.subdivision?.detailWeight ?? 1.0) === p.value;
                    return (
                      <button
                        key={p.value}
                        type="button"
                        onClick={() => void handleDetailWeight(selectedObject, p.value)}
                        disabled={busy}
                        className={`rounded px-1 py-1 text-center transition-colors ${
                          active
                            ? "bg-[#087f74] text-white font-bold"
                            : "bg-white text-[#526460] border border-[#d6dfdc] hover:bg-[#edf6f2]"
                        }`}
                        title={p.desc}
                      >
                        <span className="block text-[11px] leading-tight">{p.label}</span>
                        <span className="block text-[9px] opacity-75">{p.value}x</span>
                      </button>
                    );
                  })}
                </div>
                <p className="text-[9px] text-[#778481] leading-tight">
                  Adjusts tap region budgeting in auto-subdivide without altering visual artwork.
                </p>
              </div>

              {/* AI Targeted Regeneration — bound to the displayed revision
                  (Task 32 review R4). Enabled only when the active AI draft is
                  the one this revision's provenance points at; the paid call
                  fires only after the explicit confirmation dialog. */}
              {(activeSession || linkedToRevision) && (
                <div className="pt-2 border-t border-[#e1e8e5] space-y-1.5">
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-semibold text-[#657671] uppercase tracking-wider">
                      AI Generation
                    </span>
                    {selectedObject.generation?.locked && (
                      <span className="text-[9px] text-amber-700 font-medium">Unlock to regenerate</span>
                    )}
                  </div>
                  <Button
                    size="sm"
                    variant="outline"
                    className="w-full h-7 text-xs bg-white text-[#087f74] border-[#cce7dc] hover:bg-[#e5f3ed] gap-1.5"
                    onClick={() => setRegenDialogOpen(true)}
                    disabled={busy || !!selectedObject.generation?.locked || !linkedToRevision}
                  >
                    <Sparkles className="size-3.5" />
                    Regenerate "{selectedObject.name}"
                  </Button>
                  {selectedObject.generation?.locked ? (
                    <p className="text-[9px] text-amber-700 leading-tight">
                      This object is locked, so AI regeneration is refused — in the draft and in
                      the committed artwork.
                    </p>
                  ) : !linkedToRevision ? (
                    <p className="text-[9px] text-[#778481] leading-tight">
                      Targeted regeneration needs an active AI draft derived from THIS revision.
                      Unrelated drafts are never reused — resume the AI workspace from this
                      revision to regenerate its objects.
                    </p>
                  ) : null}
                </div>
              )}

              {/* Generation Provenance info */}
              {selectedObject.generation?.prompt && (
                <div className="rounded bg-[#f0f4f1] p-2 text-[10px] text-[#526460] space-y-0.5">
                  <span className="font-semibold block text-[#364843]">Prompt provenance:</span>
                  <p className="italic text-[#5e716c] line-clamp-2">{selectedObject.generation.prompt}</p>
                </div>
              )}
            </div>
          ) : (
            <p className="text-center text-[11px] text-[#778481] py-2">
              Select an object in the tree to inspect details and layer controls.
            </p>
          )}
        </div>
      )}

      {/* Paid-action confirmation (Task 32 review R4): instructions + explicit
          consent BEFORE the provider is called — the established creation
          workspace experience, zero provider calls until confirmed. */}
      <AlertDialog open={regenDialogOpen} onOpenChange={setRegenDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Regenerate "{selectedObject?.name}" with AI?</AlertDialogTitle>
            <AlertDialogDescription>
              This replaces the object's shapes in the linked AI draft with a freshly generated
              fragment (objectId is preserved) and may incur provider charges. Confirm to send the
              request.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-2.5">
            <Label htmlFor="regen-instructions" className="text-[11px] text-[#183837]">
              Change instructions (optional)
            </Label>
            <Textarea
              id="regen-instructions"
              value={regenInstructions}
              onChange={(e) => setRegenInstructions(e.target.value)}
              placeholder="e.g. make the roof steeper and darker"
              className="min-h-[60px] text-xs"
            />
            <div className="flex items-start gap-2">
              <Checkbox
                id="regen-paid-consent"
                checked={regenPaidConsent}
                onCheckedChange={(v) => setRegenPaidConsent(v === true)}
              />
              <Label htmlFor="regen-paid-consent" className="text-[11px] font-normal leading-snug text-[#526460]">
                I understand this sends a paid request to the AI provider for
                "{selectedObject?.name}".
              </Label>
            </div>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={!regenPaidConsent || busy}
              onClick={(e) => {
                e.preventDefault();
                if (selectedObject) void handleRegenerate(selectedObject);
              }}
            >
              Regenerate object
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
