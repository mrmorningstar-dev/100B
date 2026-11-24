import React from 'react';
import { Shield, ArrowLeft, ExternalLink, Lock, CheckCircle2 } from 'lucide-react';

interface PrivacyPolicyViewProps {
  onBackToDashboard?: () => void;
}

export const PrivacyPolicyView: React.FC<PrivacyPolicyViewProps> = ({ onBackToDashboard }) => {
  return (
    <div className="min-h-screen bg-[#EDF0EC] text-[#1D3B33] font-ui p-[24px] lg:p-[48px] flex flex-col items-center">
      <div className="w-full max-w-[860px] flex flex-col gap-[28px]">
        {/* Navigation & Header */}
        <div className="flex items-center justify-between pb-[16px] border-b border-[#D5DBD5]/60">
          <button
            type="button"
            onClick={() => {
              if (onBackToDashboard) {
                onBackToDashboard();
              } else {
                window.location.href = '/';
              }
            }}
            className="flex items-center gap-[8px] px-[14px] py-[8px] rounded-[10px] bg-white border border-[#D5DBD5] text-[#1D3B33] font-medium text-[13.5px] hover:bg-[#EDF0EC] transition-colors cursor-pointer"
          >
            <ArrowLeft className="size-[16px]" />
            <span>Back to Dashboard</span>
          </button>

          <div className="flex items-center gap-[10px]">
            <img src="/avatar.png" alt="Betterverse Logo" className="size-[32px] rounded-full object-cover border border-[#D5DBD5]" />
            <span className="font-heading font-bold text-[16px] text-[#1D3B33]">Betterverse</span>
          </div>
        </div>

        {/* Title banner */}
        <div className="bg-white rounded-[20px] p-[32px] lg:p-[40px] border border-[#D5DBD5] shadow-xs flex flex-col gap-[16px]">
          <div className="inline-flex items-center gap-[8px] px-[12px] py-[6px] rounded-full bg-[#2E7D4F]/10 border border-[#2E7D4F]/30 text-[#2E7D4F] font-semibold text-[12px] uppercase tracking-wider w-fit">
            <Shield className="size-[14px]" />
            <span>Legal Documentation</span>
          </div>

          <h1 className="font-heading font-extrabold text-[32px] lg:text-[40px] text-[#1D3B33] tracking-tight leading-none">
            Privacy Policy
          </h1>
          <p className="text-[14px] text-[#6F7A73]">
            Last updated: November 24, 2026 · Effective immediately for Betterverse &amp; 100B Automated Pipeline
          </p>

          <hr className="border-[#D5DBD5]/60 my-[8px]" />

          {/* Policy Content */}
          <div className="flex flex-col gap-[24px] text-[14.5px] text-[#33463F] leading-relaxed">
            <section className="flex flex-col gap-[8px]">
              <h2 className="font-heading font-bold text-[18px] text-[#1D3B33]">1. Introduction &amp; Scope</h2>
              <p>
                This Privacy Policy explains how <strong>Betterverse</strong> ("we", "our", or "the Service"), operating the 100B Automated YouTube Shorts curation and broadcasting system, accesses, stores, and protects user data and YouTube API credentials.
              </p>
              <p>
                We are committed to full transparency and strict compliance with the{' '}
                <a
                  href="https://developers.google.com/terms/api-services-user-data-policy"
                  target="_blank"
                  rel="noreferrer"
                  className="text-[#2E7D4F] font-semibold hover:underline inline-flex items-center gap-[4px]"
                >
                  Google API Services User Data Policy
                  <ExternalLink className="size-[12px]" />
                </a>{' '}
                and the{' '}
                <a
                  href="https://www.youtube.com/t/terms"
                  target="_blank"
                  rel="noreferrer"
                  className="text-[#2E7D4F] font-semibold hover:underline inline-flex items-center gap-[4px]"
                >
                  YouTube Terms of Service
                  <ExternalLink className="size-[12px]" />
                </a>.
              </p>
            </section>

            <section className="flex flex-col gap-[8px]">
              <h2 className="font-heading font-bold text-[18px] text-[#1D3B33]">2. Google &amp; YouTube API Data We Access</h2>
              <p>
                When you authorize Betterverse to connect to your YouTube Channel via Google OAuth 2.0, our application requests the minimum required scopes necessary to curate and upload videos:
              </p>
              <ul className="list-disc pl-[24px] space-y-[4px]">
                <li><strong>https://www.googleapis.com/auth/youtube.upload:</strong> Used solely to upload scheduled video shorts to your authorized YouTube channel.</li>
                <li><strong>https://www.googleapis.com/auth/youtube.readonly:</strong> Used to inspect publish status, view counts, Content ID claims, and privacy state of uploaded videos.</li>
                <li><strong>https://www.googleapis.com/auth/youtube:</strong> Used to update publication schedules, video descriptions, titles, and tags.</li>
              </ul>
            </section>

            <section className="flex flex-col gap-[8px]">
              <h2 className="font-heading font-bold text-[18px] text-[#1D3B33]">3. How Data Is Stored and Secured</h2>
              <div className="p-[16px] rounded-[12px] bg-[#EDF0EC] border border-[#D5DBD5] flex items-start gap-[12px]">
                <Lock className="size-[20px] text-[#1D3B33] shrink-0 mt-[2px]" />
                <div className="flex flex-col gap-[4px] text-[13.5px]">
                  <span className="font-bold text-[#1D3B33]">Local Security Architecture</span>
                  <p className="text-[#495A52]">
                    All Google OAuth access and refresh tokens are stored exclusively on the operator's private PC in encrypted directories. OAuth tokens are <strong>never</strong> transmitted to third parties, never uploaded to Vercel, and never stored in cloud databases.
                  </p>
                </div>
              </div>
            </section>

            <section className="flex flex-col gap-[8px]">
              <h2 className="font-heading font-bold text-[18px] text-[#1D3B33]">4. Limited Use &amp; Zero Data Sharing</h2>
              <p>
                We do not sell, rent, or monetize your Google user data under any circumstances. Specifically:
              </p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-[12px] mt-[4px]">
                <div className="p-[14px] rounded-[10px] bg-white border border-[#D5DBD5]/80 flex items-start gap-[8px]">
                  <CheckCircle2 className="size-[16px] text-[#2E7D4F] shrink-0 mt-[2px]" />
                  <span>No data is sold to data brokers or advertisers.</span>
                </div>
                <div className="p-[14px] rounded-[10px] bg-white border border-[#D5DBD5]/80 flex items-start gap-[8px]">
                  <CheckCircle2 className="size-[16px] text-[#2E7D4F] shrink-0 mt-[2px]" />
                  <span>No data is used to train public machine learning models.</span>
                </div>
                <div className="p-[14px] rounded-[10px] bg-white border border-[#D5DBD5]/80 flex items-start gap-[8px]">
                  <CheckCircle2 className="size-[16px] text-[#2E7D4F] shrink-0 mt-[2px]" />
                  <span>No cookies or tracking pixels are placed on third-party viewers.</span>
                </div>
                <div className="p-[14px] rounded-[10px] bg-white border border-[#D5DBD5]/80 flex items-start gap-[8px]">
                  <CheckCircle2 className="size-[16px] text-[#2E7D4F] shrink-0 mt-[2px]" />
                  <span>Data is retained strictly for the duration of channel management.</span>
                </div>
              </div>
            </section>

            <section className="flex flex-col gap-[8px]">
              <h2 className="font-heading font-bold text-[18px] text-[#1D3B33]">5. Revoking Access &amp; Data Deletion</h2>
              <p>
                You may revoke Betterverse's access to your YouTube channel at any time through Google's Account Security Portal:
              </p>
              <p>
                <a
                  href="https://myaccount.google.com/permissions"
                  target="_blank"
                  rel="noreferrer"
                  className="text-[#2E7D4F] font-semibold hover:underline inline-flex items-center gap-[4px]"
                >
                  Google Account Third-Party Permissions
                  <ExternalLink className="size-[12px]" />
                </a>
              </p>
              <p>
                To delete all locally cached tokens, simply remove the local <code className="bg-[#EDF0EC] px-[6px] py-[2px] rounded text-[12.5px]">uploader/secrets/</code> directory on your host machine.
              </p>
            </section>

            <section className="flex flex-col gap-[8px]">
              <h2 className="font-heading font-bold text-[18px] text-[#1D3B33]">6. Contact Information</h2>
              <p>
                If you have questions about this Privacy Policy or your data, please contact the developer via GitHub at{' '}
                <a
                  href="https://github.com/mrmorningstar-dev/100B"
                  target="_blank"
                  rel="noreferrer"
                  className="text-[#2E7D4F] font-semibold hover:underline"
                >
                  github.com/mrmorningstar-dev/100B
                </a>.
              </p>
            </section>
          </div>
        </div>

        {/* Footer */}
        <div className="text-center text-[12px] text-[#8A958E] pb-[24px]">
          © 2026 Betterverse · All rights reserved.
        </div>
      </div>
    </div>
  );
};
