import React, { useState, useEffect } from 'react';
import { Play } from 'lucide-react';
import { getApiUrl, getCachedMediaToken } from '../utils/api';

interface ThumbProps {
  src?: string | null;
  alt?: string;
  className?: string;
  width?: number | string;
  height?: number | string;
}

export const Thumb: React.FC<ThumbProps> = ({
  src,
  alt = 'Video thumbnail',
  className = '',
  width,
  height,
}) => {
  const [hasError, setHasError] = useState(false);
  const [token, setToken] = useState<string | null>(() => getCachedMediaToken());

  // Listen for renewed media tokens (renewed every ~8 mins by App or on unlock)
  useEffect(() => {
    const handleUpdate = (e: Event) => {
      const customEvent = e as CustomEvent<{ token: string | null }>;
      setToken(customEvent.detail?.token || null);
      setHasError(false); // Reset error state on new token so image retries
    };
    window.addEventListener('dashboard:media-token-updated', handleUpdate);
    return () => {
      window.removeEventListener('dashboard:media-token-updated', handleUpdate);
    };
  }, []);

  // Reset error state during render if the thumbnail source URL changes
  const [prevSrc, setPrevSrc] = useState(src);
  if (prevSrc !== src) {
    setPrevSrc(src);
    setHasError(false);
  }

  const styleObj: React.CSSProperties = {};
  if (width) styleObj.width = typeof width === 'number' ? `${width}px` : width;
  if (height) styleObj.height = typeof height === 'number' ? `${height}px` : height;

  const defaultClasses = width || height ? '' : 'w-[32px] h-[56px]';

  if (src && !hasError) {
    const resolvedSrc = getApiUrl(src, { isMedia: true, token });
    return (
      <div
        style={styleObj}
        className={`relative overflow-hidden rounded-[8px] bg-[#17302B] shrink-0 border border-[#D5DBD5]/30 ${defaultClasses} ${className}`}
      >
        <img
          key={resolvedSrc}
          src={resolvedSrc}
          alt={alt}
          onError={() => setHasError(true)}
          className="w-full h-full object-cover block"
          loading="lazy"
        />
      </div>
    );
  }

  // Figma gradient thumbnail fallback (rendered when no src or when token expired / loading error)
  return (
    <div
      style={styleObj}
      data-testid="thumb-fallback"
      className={`relative overflow-hidden rounded-[8px] bg-gradient-to-b from-[#335C4F] to-[#17302B] flex items-center justify-center shrink-0 border border-[#D5DBD5]/20 ${defaultClasses} ${className}`}
    >
      <Play className="size-[12px] text-white/70 fill-white/50" />
    </div>
  );
};
