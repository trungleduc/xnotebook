export interface IMountFile {
  /** Absolute path inside the kernel filesystem. */
  path: string;
  /** base64 file content. */
  data: string;
}

export interface IMount {
  /** Absolute destination directory (or file) in the kernel filesystem. */
  dst: string;
  mode: 'ro' | 'rw';
  files: IMountFile[];
  dirs: string[];
}

export interface IJob {
  /** Input file name (used for the extension and messages). */
  path: string;
  format: 'ipynb' | 'script';
  /** Raw file content. */
  content: string;
  /** Content of an environment.yaml given with -e. */
  envYaml?: string | null;
  deps: string[];
  pip: string[];
  channels: string[];
  kernel?: string | null;
  /** A lock given with --lock (skips solving). */
  lock?: any | null;
  stdin?: string[] | null;
  mounts: IMount[];
  allowErrors: boolean;
  cellTimeout?: number | null;
  /** Save ipywidgets state in metadata.widgets (default true). */
  widgetState?: boolean;
  /** Working directory of the kernel. */
  cwd?: string | null;
  /** Ask the host for cells one at a time (`next`) instead of running `content`. */
  interactive?: boolean;
  /** Relay raw Jupyter messages between the host and the kernel (`xnb kernel start`). */
  bridge?: boolean;
  /** Run an agent next to the kernel (`xnb agent`). */
  agent?: IAgentConfig | null;
}

/** `xnb agent` settings. No secrets: the host adds the API key to every model request. */
export interface IAgentConfig {
  provider: 'anthropic' | 'openai';
  model: string;
  /** OpenAI-compatible provider: the host forwards to api.openai.com itself. */
  openaiOfficial?: boolean;
  /** Reasoning effort (low ... max); null leaves it to the model. */
  effort?: string | null;
  maxTokens?: number | null;
  /** pi-ai compat settings for unusual OpenAI-compatible servers. */
  compat?: Record<string, any>;
  /** Send plots and images to the model. */
  images: boolean;
  /** Extra system prompt (--system, AGENTS.md). */
  system?: string | null;
  maxRestarts: number;
  sessionId: string;
}

/** What the agent worker is told about its sandbox when it starts. */
export interface IAgentInit {
  config: IAgentConfig;
  cwd: string;
  mounts: { dst: string; mode: 'ro' | 'rw' }[];
  kernel: { name: string; display_name: string; language?: string };
  packages: string[];
}

export type Output = Record<string, any>;

export interface ICell {
  cell_type: string;
  source: string | string[];
  metadata: Record<string, any>;
  outputs?: Output[];
  execution_count?: number | null;
  [k: string]: any;
}

export interface INotebook {
  nbformat: number;
  nbformat_minor: number;
  metadata: Record<string, any>;
  cells: ICell[];
}

export interface IRunResult {
  notebook: INotebook;
  status: 'ok' | 'error' | 'timeout';
  /** Index of the cell that failed, if any. */
  failedCell?: number | null;
  error?: string | null;
  mounts?: { dst: string; files: IMountFile[] }[];
  /** Interactive session: report of the cell that ended it (timeout or dead kernel). */
  last?: Record<string, any> | null;
}
