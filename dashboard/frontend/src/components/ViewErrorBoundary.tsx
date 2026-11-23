import { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertCircle, RotateCcw } from 'lucide-react';

interface Props {
  children: ReactNode;
  viewName?: string;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ViewErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('View error caught by boundary:', error, errorInfo);
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex-1 p-6 md:p-10 flex flex-col items-center justify-center min-h-[400px]">
          <div className="max-w-md w-full bg-white rounded-[16px] border border-[#DC2626]/30 p-6 shadow-sm flex flex-col items-start gap-4">
            <div className="flex items-center gap-3 text-[#DC2626]">
              <AlertCircle size={22} className="shrink-0" />
              <h2 className="font-heading font-semibold text-[18px] text-[#1D3B33]">
                This page failed to load
              </h2>
            </div>
            <p className="font-ui text-[13px] text-[#6F7A73] leading-relaxed">
              {this.props.viewName
                ? `An error occurred while loading the ${this.props.viewName} view.`
                : 'An unexpected error occurred while rendering this page.'}
            </p>
            {this.state.error?.message && (
              <pre className="w-full p-3 rounded-[8px] bg-[#FEE2E2] text-[#991B1B] text-[12px] font-mono overflow-x-auto whitespace-pre-wrap break-words">
                {this.state.error.message}
              </pre>
            )}
            <button
              onClick={this.handleRetry}
              className="inline-flex items-center gap-2 px-4 py-2 rounded-[8px] bg-[#2E7D4F] text-white font-ui font-medium text-[13px] hover:bg-[#25633F] transition-colors cursor-pointer"
            >
              <RotateCcw size={14} /> Retry
            </button>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
