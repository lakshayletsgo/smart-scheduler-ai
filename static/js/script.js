document.addEventListener('DOMContentLoaded', () => {
    const chatForm = document.getElementById('chat-form');
    const userInput = document.getElementById('user-input');
    const chatMessages = document.getElementById('chat-messages');
    let isProcessing = false;
    let lastTimeSlots = null;

    function addMessage(content, isUser = false) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${isUser ? 'user' : 'assistant'}`;
        
        const messageContent = document.createElement('div');
        messageContent.className = 'message-content';
        
        // Check if the content contains newlines and preserve formatting
        if (content.includes('\n')) {
            messageContent.innerHTML = content.split('\n').map(line => {
                // Preserve emojis and bullet points
                return line.replace(/^(•|📝|✅|❌|📧|🔗|📅|🕒|✨)/, '<span class="emoji">$1</span>');
            }).join('<br>');
        } else {
            messageContent.textContent = content;
        }
        
        messageDiv.appendChild(messageContent);
        chatMessages.appendChild(messageDiv);
        
        // Scroll to bottom
        chatMessages.scrollTop = chatMessages.scrollHeight;

        // If voice is enabled and it's an assistant message, speak it
        if (!isUser && window.voiceHandler && window.voiceHandler.voiceEnabled) {
            window.voiceHandler.speak(content);
        }
    }

    function createTimeSlots(slots) {
        lastTimeSlots = slots; // Store the slots for reference
        const timeSlotsDiv = document.createElement('div');
        timeSlotsDiv.className = 'time-slots';
        
        slots.forEach((slot, index) => {
            const timeSlot = document.createElement('div');
            timeSlot.className = 'time-slot';
            timeSlot.innerHTML = `<span class="slot-number">${index + 1}.</span> ${slot}`;
            timeSlot.onclick = () => selectTimeSlot(index);
            timeSlotsDiv.appendChild(timeSlot);
        });
        
        return timeSlotsDiv;
    }

    async function selectTimeSlot(index) {
        if (isProcessing) return;
        isProcessing = true;
        
        try {
            // Show selection message
            addMessage(`I'll schedule the meeting for ${lastTimeSlots[index]}`, true);
            
            const response = await fetch('/schedule', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ slot_index: index })
            });
            
            const data = await response.json();
            addMessage(data.message || (data.success ? 
                '✅ Meeting scheduled successfully!' : 
                '❌ Failed to schedule the meeting. Please try again.'));
            
            if (data.success) {
                // Clear the time slots after successful scheduling
                lastTimeSlots = null;
                // Remove all time slot elements
                document.querySelectorAll('.time-slots').forEach(el => el.remove());
            }
                
        } catch (error) {
            console.error('Error:', error);
            addMessage('❌ Sorry, there was an error scheduling the meeting. Please try again.');
        } finally {
            isProcessing = false;
        }
    }

    chatForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const message = userInput.value.trim();
        if (!message || isProcessing) return;
        
        // Check if the message is a number and might be a slot selection
        const slotNumber = parseInt(message);
        if (!isNaN(slotNumber) && lastTimeSlots && slotNumber > 0 && slotNumber <= lastTimeSlots.length) {
            userInput.value = '';
            await selectTimeSlot(slotNumber - 1);
            return;
        }
        
        // Clear input and stop listening if voice is active
        userInput.value = '';
        if (window.voiceHandler) {
            window.voiceHandler.stopListening();
        }
        
        // Add user message to chat
        addMessage(message, true);
        
        isProcessing = true;
        try {
            const response = await fetch('/chat', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ message })
            });
            
            const data = await response.json();
            
            // Add AI response to chat
            addMessage(data.response);
            
            // If there are time slots, add them
            if (data.has_calendar_data && data.time_slots) {
                // Remove any existing time slots
                document.querySelectorAll('.time-slots').forEach(el => el.remove());
                const timeSlotsElement = createTimeSlots(data.time_slots);
                chatMessages.appendChild(timeSlotsElement);
            }
        } catch (error) {
            console.error('Error:', error);
            addMessage('❌ Sorry, there was an error processing your request. Please try again.');
        } finally {
            isProcessing = false;
        }
    });

    // Add voice control buttons
    const controlsDiv = document.createElement('div');
    controlsDiv.className = 'voice-controls';
    controlsDiv.innerHTML = `
        <button id="voice-toggle" class="voice-button" title="Toggle voice mode">
            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"></path>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
                <line x1="12" y1="19" x2="12" y2="23"></line>
                <line x1="8" y1="23" x2="16" y2="23"></line>
            </svg>
        </button>
        <button id="mic-button" class="voice-button" title="Start speaking">
            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"></path>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
                <line x1="12" y1="19" x2="12" y2="23"></line>
                <line x1="8" y1="23" x2="16" y2="23"></line>
            </svg>
        </button>
    `;
    
    document.querySelector('.chat-input').appendChild(controlsDiv);

    // Voice control event listeners
    document.getElementById('voice-toggle').addEventListener('click', () => {
        if (window.voiceHandler) {
            window.voiceHandler.toggleVoice();
        }
    });

    document.getElementById('mic-button').addEventListener('click', () => {
        if (window.voiceHandler && window.voiceHandler.voiceEnabled) {
            window.voiceHandler.startListening();
        }
    });

    // Handle keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        // Press 'v' to toggle voice mode
        if (e.key === 'v' && !e.ctrlKey && !e.metaKey) {
            document.getElementById('voice-toggle').click();
        }
        // Press space to start speaking when voice mode is enabled
        if (e.code === 'Space' && window.voiceHandler && 
            window.voiceHandler.voiceEnabled && 
            document.activeElement !== userInput) {
            e.preventDefault();
            document.getElementById('mic-button').click();
        }
    });

    // Enable submit on Enter key
    userInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            chatForm.dispatchEvent(new Event('submit'));
        }
    });
}); 