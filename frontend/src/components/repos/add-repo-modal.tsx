"use client";

import { useState } from "react";
import { Modal } from "@/components/ui/modal";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { api, RepoOut } from "@/lib/api";
import { Plus, AlertCircle, Loader2 } from "lucide-react";

interface AddRepoModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: (repo: RepoOut) => void;
}

export function AddRepoModal({ isOpen, onClose, onSuccess }: AddRepoModalProps) {
  const [owner, setOwner] = useState("");
  const [name, setName] = useState("");
  const [defaultBranch, setDefaultBranch] = useState("main");
  const [languageHint, setLanguageHint] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!owner.trim() || !name.trim()) {
      setError("Owner and repository name are required.");
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const newRepo = await api.addRepo({
        owner: owner.trim(),
        name: name.trim(),
        default_branch: defaultBranch.trim() || "main",
        language_hint: languageHint.trim() || null,
      });

      // Reset fields
      setOwner("");
      setName("");
      setDefaultBranch("main");
      setLanguageHint("");
      onSuccess(newRepo);
      onClose();
    } catch (err: unknown) {
      if (err instanceof Error) {
        setError(err.message);
      } else {
        setError("Failed to connect repository. Please check permissions.");
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Connect Repository"
      description="Track GitHub Action workflow failures and generate autonomous fixes."
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        {error && (
          <div className="flex items-center gap-2 rounded-[5px] border border-red-900/60 bg-red-950/30 p-2.5 text-xs text-red-300">
            <AlertCircle className="h-4 w-4 shrink-0 text-red-400" />
            <span>{error}</span>
          </div>
        )}

        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <label className="text-[11px] font-medium text-zinc-300 uppercase tracking-wider">
              Owner / Org *
            </label>
            <Input
              placeholder="e.g. facebook"
              value={owner}
              onChange={(e) => setOwner(e.target.value)}
              disabled={loading}
              autoFocus
              required
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-[11px] font-medium text-zinc-300 uppercase tracking-wider">
              Repo Name *
            </label>
            <Input
              placeholder="e.g. react"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={loading}
              required
            />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <label className="text-[11px] font-medium text-zinc-300 uppercase tracking-wider">
              Default Branch
            </label>
            <Input
              placeholder="main"
              value={defaultBranch}
              onChange={(e) => setDefaultBranch(e.target.value)}
              disabled={loading}
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-[11px] font-medium text-zinc-300 uppercase tracking-wider">
              Language Hint
            </label>
            <Input
              placeholder="e.g. python, ts"
              value={languageHint}
              onChange={(e) => setLanguageHint(e.target.value)}
              disabled={loading}
            />
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 pt-3 border-t border-zinc-800/80">
          <Button
            type="button"
            variant="outline"
            onClick={onClose}
            disabled={loading}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            disabled={loading}
            className="flex items-center gap-1.5 bg-amber-400 text-zinc-950 hover:bg-amber-300"
          >
            {loading ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Plus className="h-3.5 w-3.5" />
            )}
            Connect Repo
          </Button>
        </div>
      </form>
    </Modal>
  );
}
