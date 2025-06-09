class VoiceHandler {
    constructor() {
        this.recognition = new (window.SpeechRecognition || window.webkitSpeechRecognition)();
        this.synthesis = window.speechSynthesis;
        this.isListening = false;
        this.isSpeaking = false;
        this.voiceEnabled = false;

        // Configure speech recognition
        this.recognition.continuous = false;
        this.recognition.interimResults = false;
        this.recognition.lang = 'en-US';

        // Setup event handlers
        this.setupRecognitionEvents();
    }

    setupRecognitionEvents() {
        this.recognition.onstart = () => {
            this.isListening = true;
            this.updateMicButton(true);
        };

        this.recognition.onend = () => {
            this.isListening = false;
            this.updateMicButton(false);
        };

        this.recognition.onerror = (event) => {
            console.error('Speech recognition error:', event.error);
            this.isListening = false;
            this.updateMicButton(false);
        };

        this.recognition.onresult = (event) => {
            const transcript = event.results[0][0].transcript;
            document.getElementById('user-input').value = transcript;
            document.getElementById('chat-form').dispatchEvent(new Event('submit'));
        };
    }

    toggleVoice() {
        this.voiceEnabled = !this.voiceEnabled;
        const voiceButton = document.getElementById('voice-toggle');
        voiceButton.classList.toggle('active', this.voiceEnabled);
        
        if (this.voiceEnabled) {
            this.speak("Voice mode activated. You can now speak your requests.");
        } else {
            this.stopSpeaking();
            this.stopListening();
        }
    }

    startListening() {
        if (!this.voiceEnabled || this.isListening) return;
        
        try {
            this.recognition.start();
        } catch (error) {
            console.error('Error starting speech recognition:', error);
        }
    }

    stopListening() {
        if (this.isListening) {
            this.recognition.stop();
        }
    }

    speak(text) {
        if (!this.voiceEnabled || this.isSpeaking) return;

        // Clean up the text for better speech
        const cleanText = this.cleanTextForSpeech(text);
        
        const utterance = new SpeechSynthesisUtterance(cleanText);
        utterance.rate = 1.0;
        utterance.pitch = 1.0;
        utterance.volume = 1.0;
        utterance.lang = 'en-US';

        // Use a more natural voice if available
        const voices = this.synthesis.getVoices();
        const preferredVoice = voices.find(voice => 
            voice.name.includes('Google') || 
            voice.name.includes('Natural') || 
            voice.name.includes('Female')
        );
        if (preferredVoice) {
            utterance.voice = preferredVoice;
        }

        utterance.onstart = () => {
            this.isSpeaking = true;
        };

        utterance.onend = () => {
            this.isSpeaking = false;
            // Start listening after speaking if voice mode is enabled
            if (this.voiceEnabled) {
                setTimeout(() => this.startListening(), 500);
            }
        };

        this.synthesis.speak(utterance);
    }

    stopSpeaking() {
        if (this.isSpeaking) {
            this.synthesis.cancel();
            this.isSpeaking = false;
        }
    }

    updateMicButton(isListening) {
        const micButton = document.getElementById('mic-button');
        if (micButton) {
            micButton.classList.toggle('listening', isListening);
            micButton.innerHTML = isListening ? 
                '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" y1="19" x2="12" y2="23"></line><line x1="8" y1="23" x2="16" y2="23"></line></svg>' :
                '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" y1="19" x2="12" y2="23"></line><line x1="8" y1="23" x2="16" y2="23"></line></svg>';
        }
    }

    cleanTextForSpeech(text) {
        // Remove emojis
        text = text.replace(/[\u{1F300}-\u{1F9FF}]/gu, '');
        
        // Remove markdown-style formatting
        text = text.replace(/[*_~`]/g, '');
        
        // Convert bullet points to pauses
        text = text.replace(/•/g, ',');
        
        // Add pauses for better speech flow
        text = text.replace(/([.!?])\s+/g, '$1, ');
        
        // Clean up any double spaces or commas
        text = text.replace(/\s+/g, ' ').replace(/,+/g, ',');
        
        return text.trim();
    }
}

// Initialize voice handler
const voiceHandler = new VoiceHandler();

// Export for use in other modules
window.voiceHandler = voiceHandler; 