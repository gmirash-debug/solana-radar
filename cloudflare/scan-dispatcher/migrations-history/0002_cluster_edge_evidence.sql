CREATE TABLE IF NOT EXISTS wallet_cluster_edge_evidence (
  edge_id TEXT NOT NULL,
  episode_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  evidence_json TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (edge_id, episode_id),
  FOREIGN KEY (edge_id) REFERENCES wallet_cluster_edges(edge_id) ON DELETE CASCADE,
  FOREIGN KEY (episode_id) REFERENCES signal_episodes(episode_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cluster_edge_evidence_episode
  ON wallet_cluster_edge_evidence (episode_id, edge_id);
