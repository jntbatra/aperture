/**
 * The chat history, always on screen at the right of the conversation.
 *
 * The same list the Explore drawer's Chats tab shows (search across every
 * question, delete, who asked what), kept open beside the thread because
 * resuming an earlier conversation is the most common reason to look for it.
 * Hidden on narrow screens, where the drawer still has it.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  deleteConversation,
  getStats,
  listConversations,
  searchTurns,
  type ConversationSummary,
  type SearchHit,
  type Stats,
} from '../api';
import { ChatList } from './Sidebar';

interface Props {
  activeId: string | null;
  /** Changes whenever a question finishes or the thread changes, to reload the list. */
  refreshKey: string;
  onOpen: (id: string) => void;
  onDeleted: (id: string) => void;
}

export function ChatsPanel({ activeId, refreshKey, onOpen, onDeleted }: Props) {
  const [conversations, setConversations] = useState<ConversationSummary[] | null>(null);
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [search, setSearch] = useState('');

  const refresh = useCallback(() => {
    listConversations().then(setConversations).catch(() => setConversations([]));
    getStats().then(setStats).catch(() => setStats(null));
  }, []);

  useEffect(refresh, [refresh, refreshKey]);

  useEffect(() => {
    const term = search.trim();
    if (!term) {
      setHits(null);
      return;
    }
    const timer = setTimeout(() => {
      searchTurns(term).then(setHits).catch(() => setHits([]));
    }, 200);
    return () => clearTimeout(timer);
  }, [search]);

  return (
    <aside className="chats-panel" aria-label="Chat history">
      <div className="chats-panel__title">Chats</div>
      <ChatList
        conversations={conversations}
        hits={hits}
        stats={stats}
        search={search}
        activeId={activeId}
        onSearch={setSearch}
        onOpen={onOpen}
        onDelete={async (id) => {
          if (!window.confirm('Delete this chat and its answers?')) return;
          await deleteConversation(id);
          setConversations((current) => (current ?? []).filter((item) => item.id !== id));
          onDeleted(id);
        }}
      />
    </aside>
  );
}
