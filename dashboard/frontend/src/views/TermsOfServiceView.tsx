import React from 'react';
import { FileText, ArrowLeft, ExternalLink, AlertTriangle } from 'lucide-react';

interface TermsOfServiceViewProps {
  onBackToDashboard?: () => void;
}

export const TermsOfServiceView: React.FC<TermsOfServiceViewProps> = ({ onBackToDashboard }) => {
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
          <div className="inline-flex items-center gap-[8px] px-[12px] py-[6px] rounded-full bg-[#1D3B33]/10 border border-[#1D3B33]/20 text-[#1D3B33] font-semibold text-[12px] uppercase tracking-wider w-fit">
            <FileText className="size-[14px]" />
            <span>Terms of Agreement</span>
          </div>

          <h1 className="font-heading font-extrabold text-[32px] lg:text-[40px] text-[#1D3B33] tracking-tight leading-none">
            Terms of Service
          </h1>
          <p className="text-[14px] text-[#6F7A73]">
            Last updated: November 24, 2026 · Effective immediately for Betterverse &amp; 100B Automated Pipeline
          </p>

          <hr className="border-[#D5DBD5]/60 my-[8px]" />

          {/* Terms Content */}
          <div className="flex flex-col gap-[24px] text-[14.5px] text-[#33463F] leading-relaxed">
            <section className="flex flex-col gap-[8px]">
              <h2 className="font-heading font-bold text-[18px] text-[#1D3B33]">1. Agreement to Terms</h2>
              <p>
                By accessing or using the <strong>Betterverse</strong> dashboard, software, and 100B automation suite (collectively, "the Service"), you agree to be bound by these Terms of Service. If you disagree with any part of these terms, you may not access or use the Service.
              </p>
            </section>

            <section className="flex flex-col gap-[8px]">
              <h2 className="font-heading font-bold text-[18px] text-[#1D3B33]">2. YouTube Terms of Service Agreement</h2>
              <p>
                Betterverse integrates with YouTube API Services to manage video scheduling, uploads, and channel metadata. By utilizing this Service, you acknowledge and agree that you are also bound by the:
              </p>
              <div className="p-[14px] rounded-[10px] bg-[#EDF0EC] border border-[#D5DBD5] flex items-center justify-between">
                <span className="font-semibold text-[#1D3B33]">YouTube Terms of Service</span>
                <a
                  href="https://www.youtube.com/t/terms"
                  target="_blank"
                  rel="noreferrer"
                  className="text-[#2E7D4F] font-semibold hover:underline inline-flex items-center gap-[4px] text-[13.5px]"
                >
                  View YouTube Terms
                  <ExternalLink className="size-[12px]" />
                </a>
              </div>
            </section>

            <section className="flex flex-col gap-[8px]">
              <h2 className="font-heading font-bold text-[18px] text-[#1D3B33]">3. Description of Service</h2>
              <p>
                Betterverse provides an automated media curation, content moderation, scheduling, and YouTube Shorts distribution pipeline. The dashboard serves as a monitoring and operator control interface for video queues, rights compliance verification, and broadcast rundown management.
              </p>
            </section>

            <section className="flex flex-col gap-[8px]">
              <h2 className="font-heading font-bold text-[18px] text-[#1D3B33]">4. Content Rights &amp; Copyright Compliance</h2>
              <p>
                Operators and users of the Service are strictly required to adhere to copyright laws and intellectual property rights:
              </p>
              <ul className="list-disc pl-[24px] space-y-[4px]">
                <li>All uploaded clips must comply with YouTube's Community Guidelines and Fair Use criteria.</li>
                <li>The Service includes automated rights-checking layers (audio fingerprinting, visual matching, and OCR prescreening) to prevent distribution of claimed or non-cleared media.</li>
                <li>If a Content ID claim or copyright strike is detected, the Service enforces automated hold and replacement procedures to protect channel standing.</li>
              </ul>
            </section>

            <section className="flex flex-col gap-[8px]">
              <h2 className="font-heading font-bold text-[18px] text-[#1D3B33]">5. Disclaimer of Warranties</h2>
              <div className="p-[16px] rounded-[12px] bg-[#D99A1E]/10 border border-[#D99A1E]/30 flex items-start gap-[12px]">
                <AlertTriangle className="size-[20px] text-[#D99A1E] shrink-0 mt-[2px]" />
                <div className="flex flex-col gap-[4px] text-[13px] text-[#523B0F]">
                  <span className="font-bold text-[#1D3B33]">"As-Is" Service Provision</span>
                  <p>
                    The Service is provided on an "AS IS" and "AS AVAILABLE" basis without warranties of any kind, whether express or implied. We do not warrant that the pipeline will operate uninterrupted, error-free, or meet any specific algorithmic performance on YouTube.
                  </p>
                </div>
              </div>
            </section>

            <section className="flex flex-col gap-[8px]">
              <h2 className="font-heading font-bold text-[18px] text-[#1D3B33]">6. Limitation of Liability</h2>
              <p>
                In no event shall Betterverse, its developers, or contributors be liable for any indirect, incidental, special, consequential, or punitive damages, including loss of revenue, YouTube channel actions, or data loss arising from the use of this software.
              </p>
            </section>

            <section className="flex flex-col gap-[8px]">
              <h2 className="font-heading font-bold text-[18px] text-[#1D3B33]">7. Termination &amp; Contact</h2>
              <p>
                We reserve the right to suspend or terminate access to the dashboard or automated pipeline at any time without prior notice.
              </p>
              <p>
                For legal inquiries or questions concerning these Terms, visit our repository at{' '}
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
